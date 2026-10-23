import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import create_app
from catalog import CatalogStore
from test_app import snapshot
from tmdb import TMDBClient, TMDBError, enrich, fingerprint, search_terms, lgbti_keywords


def movie_details():
    return {
        "id": 63, "title": "12 monos", "original_title": "Twelve Monkeys",
        "release_date": "1995-12-29", "overview": "Sinopsis de prueba en español.",
        "runtime": 130, "original_language": "en", "genres": [{"name": "Ciencia ficción"}],
        "production_countries": [{"name": "Estados Unidos"}],
        "production_companies": [{"name": "Universal Pictures"}],
        "vote_average": 7.6, "vote_count": 9000, "imdb_id": "tt0114746",
        "backdrop_path": "/poster.jpg",
        "credits": {
            "crew": [{"name": "Terry Gilliam", "job": "Director"}, {"name": "Other", "job": "Producer"}],
            "cast": [{"name": "Bruce Willis", "character": "James Cole"}],
        },
        "videos": {"results": [
            {"site": "YouTube", "type": "Trailer", "key": "nsuk8ThvnpM", "official": False},
        ]},
        "watch/providers": {"results": {
            "CA": {"flatrate": [{"provider_name": "Crave"}], "rent": [{"provider_name": "YouTube"}]},
            "US": {"flatrate": [{"provider_name": "Not Canadian"}]},
        }},
    }


class TMDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "tmdb.local.json"
        self.config.write_text(json.dumps({"read_access_token": "private-test-token", "api_key": ""}))
        self.store = CatalogStore(Path(self.temp.name) / "catalog.db")
        data = snapshot()
        data["movies"][0].update(name="12 Monkeys (Twelve Monkeys) 4K (1995)", year="1995")
        self.original = data["movies"][0].copy()
        self.store.replace(data)
        self.client = TMDBClient(self.config)

    def test_normalization_and_multilingual_search(self):
        self.assertEqual(search_terms(self.original), (["12 Monkeys", "Twelve Monkeys"], "1995"))
        self.assertEqual(search_terms({"name": "Árbol HD", "releaseDate": "2001-01-01"}), (["Árbol"], "2001"))
        candidate = {"id": 63, "title": "12 monos", "original_title": "Twelve Monkeys", "release_date": "1995-12-29"}
        with patch.object(self.client, "get", return_value={"results": [candidate]}) as get:
            candidates, selected = self.client.search("movies", self.original)
        self.assertEqual(selected, 63)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[1]["language"], "es-ES")
        self.assertEqual(get.call_args.args[1]["year"], "1995")
        self.assertNotIn("cast", get.call_args.args[1])

    def test_keyword_labels_for_movies_and_series_without_extra_requests(self):
        for kind, field in (("movies", "keywords"), ("series", "results")):
            data = movie_details()
            data["keywords"] = {field: [
                {"id": 1, "name": "gay interest"}, {"id": 2, "name": "friendship"},
                {"id": 3, "name": "transgender"},
            ]}
            with patch.object(self.client, "get", return_value=data) as get:
                result = self.client.details(kind, 63)
            get.assert_called_once()
            self.assertIn("keywords", get.call_args.args[1]["append_to_response"].split(","))
            self.assertEqual(result["lgbti_keywords"], ["gay interest", "transgender"])
            self.assertIn("friendship", result["keywords"])
        self.assertEqual(lgbti_keywords(["family", "gayle", "coming out", "friendship"]), [])

    def test_content_label_at_start_cached_async_and_unknown(self):
        app = create_app(self.store.path)
        app.testing = True
        client = app.test_client()
        identity = fingerprint(self.original)
        payload = {"status": "matched", "id": 63, "media": "movie",
                   "lgbti_keywords": ["gay interest"], "keywords": ["gay interest"]}
        self.store.save_tmdb("movies", "10", "CA", identity, payload)
        html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn('badge text-bg-danger', html)
        self.assertLess(html.index("Temática LGBTI"), html.index('class="table-responsive'))
        self.assertIn("gay interest", html)
        result = client.get("/api/tmdb/movies/10").json
        self.assertIn("Temática LGBTI", result["content_labels"])
        for payload in (
            {"status": "matched", "id": 63, "media": "movie"},
            {"status": "matched", "id": 63, "media": "movie", "lgbti_keywords": []},
            {"status": "candidates", "candidates": [], "lgbti_keywords": ["gay interest"]},
        ):
            self.store.save_tmdb("movies", "10", "CA", identity, payload)
            html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
            self.assertNotIn("Temática LGBTI", html)
            self.assertNotIn("Temática LGBTI", client.get("/api/tmdb/movies/10").json["content_labels"])

    def test_ambiguous_missing_year_and_tv_search(self):
        candidates = [
            {"id": 1, "name": "Batman", "original_name": "Batman", "first_air_date": "1995-01-01"},
            {"id": 2, "name": "Batman", "original_name": "Batman", "first_air_date": "1995-02-01"},
        ]
        with patch.object(self.client, "get", return_value={"results": candidates}) as get:
            _, selected = self.client.search("series", {"name": "Batman", "year": "1995"})
            self.assertIsNone(selected)
            self.assertEqual(get.call_args.args[0], "search/tv")
            self.assertEqual(get.call_args.args[1]["first_air_date_year"], "1995")
        with patch.object(self.client, "get", return_value={"results": candidates[:1]}):
            self.assertIsNone(self.client.search("series", {"name": "Batman"})[1])
            self.assertIsNone(self.client.search("series", {"name": "Batman", "year": "2000"})[1])

    def test_details_project_fields_and_country(self):
        with patch.object(self.client, "get", return_value=movie_details()):
            data = self.client.details("movies", 63)
        self.assertEqual(data["directors"], ["Terry Gilliam"])
        self.assertEqual(data["runtime"], 130)
        self.assertEqual(data["providers"]["flatrate"], ["Crave"])
        self.assertEqual(data["cast"][0]["character"], "James Cole")
        self.assertEqual(data["backdrop_url"], "https://image.tmdb.org/t/p/w780/poster.jpg")
        self.assertEqual(data["trailer_url"], "https://www.youtube.com/watch?v=nsuk8ThvnpM")
        self.assertFalse(data["trailer_official"])
        source = movie_details()
        source.update(backdrop_path="//evil.invalid/image", imdb_id='javascript:bad')
        source["videos"]["results"][0]["key"] = "javascript:bad"
        with patch.object(self.client, "get", return_value=source):
            data = self.client.details("series", 63)
        self.assertIsNone(data["backdrop_url"])
        self.assertIsNone(data["trailer_url"])
        self.assertEqual(data["media"], "tv")

    def test_cache_is_persistent_and_raw_source_is_untouched(self):
        client = Mock()
        client.search.return_value = ([], 63)
        with patch.object(self.client, "get", return_value=movie_details()):
            client.details.return_value = self.client.details("movies", 63)
        first = enrich(self.store, "movies", "10", client_factory=lambda: client)
        self.assertEqual(first["country"], "CA")
        factory = Mock(side_effect=AssertionError("Cache should avoid the network"))
        second = enrich(CatalogStore(self.store.path), "movies", "10", client_factory=factory)
        self.assertEqual(first, second)
        factory.assert_not_called()
        self.assertEqual(self.store.detail("movies", "10")["data"], self.original)
        client.details.side_effect = TMDBError("No disponible")
        with self.assertRaises(TMDBError):
            enrich(self.store, "movies", "10", client_factory=lambda: client, refresh=True)
        self.assertEqual(self.store.tmdb_cached("movies", "10", "CA", fingerprint(self.original)), first)
        self.assertIsNone(self.store.tmdb_cached("movies", "10", "US", fingerprint(self.original)))
        changed = {**self.original, "name": "Different Movie"}
        self.assertNotEqual(fingerprint(changed), fingerprint(self.original))
        snapshot_data = snapshot()
        snapshot_data["movies"][0] = changed
        self.store.replace(snapshot_data)
        client.search.return_value = ([], None)
        invalidated = enrich(self.store, "movies", "10", client_factory=lambda: client)
        self.assertEqual(invalidated["status"], "missing")
        self.assertEqual(self.store.detail("movies", "10")["data"], changed)

    def test_manual_selection_requires_current_candidates(self):
        client = Mock()
        client.search.return_value = ([{"id": 63, "title": "12 monos", "original_title": "Twelve Monkeys", "date": "1995"}], None)
        result = enrich(self.store, "movies", "10", client_factory=lambda: client)
        self.assertEqual(result["status"], "candidates")
        with self.assertRaises(TMDBError):
            enrich(self.store, "movies", "10", client_factory=lambda: client, selected=999)
        with patch.object(self.client, "get", return_value=movie_details()):
            client.details.return_value = self.client.details("movies", 63)
        result = enrich(self.store, "movies", "10", client_factory=lambda: client, selected=63)
        self.assertEqual(result["status"], "matched")
        client.search.assert_called_once()

    def test_missing_result_is_cached(self):
        client = Mock()
        client.search.return_value = ([], None)
        self.assertEqual(enrich(self.store, "movies", "10", client_factory=lambda: client)["status"], "missing")
        enrich(self.store, "movies", "10", client_factory=lambda: client)
        client.search.assert_called_once()

    def test_credentials_and_http_failures_never_leak_secrets(self):
        with patch("tmdb.requests.get", return_value=Mock(status_code=401)) as get:
            with self.assertRaises(TMDBError) as error:
                self.client.get("search/movie", {"query": "Test"})
            self.assertNotIn("private-test-token", str(error.exception))
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer private-test-token")
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with patch("tmdb.requests.get", side_effect=requests.Timeout("private-test-token")):
            with self.assertRaises(TMDBError) as error:
                self.client.get("search/movie")
            self.assertNotIn("private-test-token", str(error.exception))
        with patch("tmdb.requests.get", return_value=Mock(status_code=200, json=Mock(side_effect=ValueError))):
            with self.assertRaises(TMDBError):
                self.client.get("search/movie")
        with patch("tmdb.requests.get", side_effect=requests.exceptions.SSLError("private-test-token")), patch("tmdb.sys.platform", "darwin"), patch("tmdb.subprocess.run", return_value=Mock(returncode=0, stdout='{"results": []}\n200')) as run:
            self.assertEqual(self.client.get("search/movie"), {"results": []})
            self.assertNotIn("private-test-token", str(run.call_args.args))
            self.assertNotIn("--insecure", run.call_args.args[0])
            self.assertNotIn("-k", run.call_args.args[0])
        self.config.write_text('{"api_key": 1}')
        with self.assertRaises(TMDBError):
            TMDBClient(self.config)

    def test_routes_auto_load_cached_data_csrf_candidates_and_errors(self):
        app = create_app(self.store.path)
        client = Mock()
        candidate = {"id": 63, "title": "<script>12 monos</script>", "original_title": "Twelve Monkeys", "date": "1995"}
        client.search.return_value = ([candidate], None)
        app.config["TMDB_CLIENT_FACTORY"] = lambda: client
        browser = app.test_client()
        detail = browser.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn('id="tmdb-section"', detail)
        self.assertIn("/static/tmdb.js", detail)
        self.assertIn("JustWatch", detail)
        self.assertIn("not endorsed or certified by TMDB", detail)
        client.search.assert_not_called()
        url = "/api/tmdb/movies/10"
        response = browser.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;script&gt;12 monos&lt;/script&gt;", response.get_json()["html"])
        self.assertEqual(browser.post(url, data={"selected": "63"}).status_code, 400)
        with browser.session_transaction() as session:
            csrf = session["tmdb_csrf"]
        with patch.object(self.client, "get", return_value=movie_details()):
            client.details.return_value = self.client.details("movies", 63)
        with self.assertLogs(app.logger, level="WARNING"):
            self.assertEqual(browser.post(url, data={"csrf": csrf, "selected": "999"}).status_code, 502)
        response = browser.post(url, data={"csrf": csrf, "selected": "63"})
        self.assertEqual(response.status_code, 200)
        html = response.get_json()["html"]
        self.assertIn("Crave", html)
        self.assertIn("Sinopsis de prueba", html)
        self.assertNotIn("Not Canadian", html)
        self.assertIn("No marcado como oficial", html)
        self.assertIn("tmdb-backdrop", html)
        self.assertEqual(browser.get(url).get_json()["html"], html)
        self.assertEqual(browser.get("/api/tmdb/channels/1").status_code, 404)
        self.assertEqual(browser.get("/api/tmdb/movies/no-such-id").status_code, 404)
        self.assertNotIn('id="tmdb-section"', browser.get("/catalog/channels/detail?id=1").get_data(as_text=True))
        client.search.side_effect = TMDBError("No se pudo conectar.")
        with self.assertLogs(app.logger, level="WARNING"):
            error = browser.post(url, data={"csrf": csrf})
        self.assertEqual(error.status_code, 502)
        self.assertIn("No se pudo conectar.", error.get_json()["error"])
        self.assertEqual(browser.get(url).status_code, 200)
        self.assertEqual(self.store.detail("movies", "10")["data"], self.original)


if __name__ == "__main__":
    unittest.main()
