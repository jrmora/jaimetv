import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot
from tmdb import TMDBClient, TMDBError, TMDBRateLimitError, recommended_titles


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        for kind, key in (("movies", "stream_id"), ("series", "series_id")):
            self.data[kind] = [
                {key: number, "name": f"Title {number}"} for number in range(1, 10)
            ]
        self.store.replace(self.data)
        for kind in ("movies", "series"):
            for number in range(1, 10):
                self.cache(kind, str(number), number)
        self.remote = Mock()
        self.remote.recommendations.return_value = [1, 3, 2, 4, 5, 6, 7, 8, 9, 999]
        self.factory = Mock(return_value=self.remote)
        self.app.config["TMDB_CLIENT_FACTORY"] = self.factory
        self.client = self.app.test_client()

    def cache(self, kind, record_id, tmdb_id, status="matched"):
        raw = self.store.detail(kind, record_id)["data"]
        self.store.save_tmdb(kind, record_id, "CA", fingerprint(raw), {
            "status": status, "id": tmdb_id, "release_date": "2020-01-01",
        })

    def test_ranked_local_intersection_deduplicates_excludes_current_and_banned(self):
        self.cache("movies", "4", 3)
        self.store.set_movie_banned("2", True)
        self.cache("movies", "5", 2)
        items = recommended_titles(self.store, "movies", "1", self.factory)
        self.assertEqual([item["id"] for item in items], ["3", "6", "7", "8", "9"])
        self.remote.recommendations.assert_called_once_with("movies", 1)
        self.assertEqual(items[0]["year"], 2020)

    def test_six_results_and_persistent_cache_rechecks_local_preferences(self):
        items = recommended_titles(self.store, "movies", "1", self.factory)
        self.assertEqual([item["id"] for item in items], ["3", "2", "4", "5", "6", "7"])
        reopened = CatalogStore(self.path)
        reopened.set_movie_banned("3", True)
        items = recommended_titles(reopened, "movies", "1", self.factory)
        self.assertEqual(items[0]["id"], "2")
        self.factory.assert_called_once()
        self.assertEqual(len(items), 6)

    def test_blocked_metadata_is_not_revalidated_for_every_candidate(self):
        self.store.set_movie_banned("2", True)
        original_connect = self.store.connect
        calls = []

        @contextmanager
        def counted_connect():
            with original_connect() as db:
                def identity(raw):
                    calls.append(raw)
                    return fingerprint(json.loads(raw))
                db.create_function("TITLE_FINGERPRINT", 1, identity)
                yield db

        with patch.object(self.store, "connect", counted_connect):
            self.store.recommended_titles("movies", 1, list(range(1, 10)))
        self.assertLessEqual(len(calls), 20)

    def test_expiration_empty_cache_and_failure(self):
        self.store.save_recommendations("movies", 1, [2], time.time() - 8 * 86400)
        self.remote.recommendations.side_effect = TMDBError("No disponible")
        response = self.client.get("/api/tmdb/movies/1/recommendations")
        self.assertEqual(response.status_code, 502)
        self.assertIn("No disponible", response.json["error"])
        self.assertIsNone(self.store.cached_recommendations("movies", 1, time.time() - 7 * 86400))
        self.remote.recommendations.side_effect = None
        self.remote.recommendations.return_value = []
        for _ in range(2):
            response = self.client.get("/api/tmdb/movies/1/recommendations")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["html"].strip(), "")
        self.assertEqual(self.remote.recommendations.call_count, 2)

    def test_unconfirmed_or_stale_metadata_never_fetches(self):
        for status in ("missing", "candidates"):
            self.cache("movies", "1", 1, status)
            self.assertEqual(recommended_titles(self.store, "movies", "1", self.factory), [])
        self.cache("movies", "1", 1)
        self.data["movies"][0]["name"] = "Changed identity"
        self.store.replace(self.data)
        self.assertEqual(recommended_titles(self.store, "movies", "1", self.factory), [])
        self.factory.assert_not_called()

    def test_unconfirmed_and_stale_targets_excluded_and_series_separate(self):
        self.cache("movies", "3", 3, "candidates")
        self.data["movies"][1]["name"] = "Changed identity"
        self.store.replace(self.data)
        items = recommended_titles(self.store, "movies", "1", self.factory)
        self.assertNotIn("2", [item["id"] for item in items])
        self.assertNotIn("3", [item["id"] for item in items])
        items = recommended_titles(self.store, "series", "1", self.factory)
        self.assertEqual([item["id"] for item in items], ["3", "2", "4", "5", "6", "7"])
        self.remote.recommendations.assert_called_with("series", 1)

    def test_route_markup_and_rate_limit(self):
        response = self.client.get("/api/tmdb/movies/1/recommendations")
        self.assertEqual(response.status_code, 200)
        self.assertIn('/catalog/movies/detail?id=3', response.json["html"])
        self.assertEqual(self.client.get("/api/tmdb/channels/1/recommendations").status_code, 404)
        self.assertEqual(self.client.get("/api/tmdb/movies/999/recommendations").status_code, 404)
        self.remote.recommendations.side_effect = TMDBRateLimitError(30)
        response = self.client.get("/api/tmdb/series/1/recommendations")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")
        page = self.client.get("/catalog/movies/detail?id=1").get_data(as_text=True)
        self.assertIn("También te podrían gustar", page)
        self.assertIn("recommendations.js", page)

    def test_client_endpoint_validation_and_adult_exclusion(self):
        client = object.__new__(TMDBClient)
        for kind, media in (("movies", "movie"), ("series", "tv")):
            with patch.object(client, "get", return_value={"results": [
                {"id": 2}, {"id": 2}, {"id": 3, "adult": True},
            ]}) as get:
                self.assertEqual(client.recommendations(kind, 1), [2])
                get.assert_called_once_with(f"{media}/1/recommendations", {"language": "es-ES", "page": 1})
        for result in ({}, {"results": None}, {"results": [{"id": "2"}]}, {"results": [{"id": True}]}):
            with patch.object(client, "get", return_value=result), self.assertRaises(TMDBError):
                client.recommendations("movies", 1)
