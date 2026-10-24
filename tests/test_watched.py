import copy
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import Mock

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot


class WatchedMoviesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.store.replace(self.data)
        self.client = self.app.test_client()
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def mark(self, record_id="10", watched="1", csrf=None):
        return self.client.post(
            f"/catalog/movies/{record_id}/watched",
            data={"watched": watched, "csrf": self.csrf if csrf is None else csrf},
            headers={"Accept": "application/json"},
        )

    def test_filter_defaults_toggle_and_persistence(self):
        self.assertEqual(self.store.query("movies", watched="seen")["total"], 0)
        self.assertEqual(self.store.query("movies", watched="unseen")["total"], 4)
        original = self.store.detail("movies", "10")["data"]
        self.assertEqual(self.mark().json, {"id": "10", "watched": True})
        self.assertEqual(self.mark().status_code, 200)
        reopened = CatalogStore(self.path)
        self.assertTrue(reopened.detail("movies", "10")["watched"])
        seen = reopened.query("movies", watched="seen")
        self.assertEqual([item["id"] for item in seen["items"]], ["10"])
        self.assertTrue(seen["items"][0]["watched"])
        self.assertEqual(reopened.query("movies", watched="unseen")["total"], 3)
        self.assertEqual(reopened.query("movies")["total"], 4)
        self.assertEqual(reopened.detail("movies", "10")["data"], original)
        self.assertEqual(self.mark(watched="0").status_code, 200)
        self.assertFalse(reopened.detail("movies", "10")["watched"])
        self.assertEqual(reopened.query("movies", watched="seen")["total"], 0)

    def test_web_defaults_to_unseen_movies_but_all_is_still_available(self):
        self.mark()
        for path in ("/movies", "/kids/movies", "/movies?foreign=1"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('id="watched-unseen" value="unseen" checked', html)
        payload = self.client.get("/api/catalog/movies?view=cards").json
        self.assertEqual(payload["total"], 3)
        self.assertNotIn("10", [item["id"] for item in payload["items"]])
        payload = self.client.get("/api/catalog/movies?view=cards&watched=all").json
        self.assertEqual(payload["total"], 4)
        html = self.client.get("/movies?watched=all").get_data(as_text=True)
        self.assertIn('id="watched-all" value="all" checked', html)
        self.assertEqual(self.client.get("/series").status_code, 200)
        self.assertEqual(self.client.get("/channels").status_code, 404)

    def test_sync_preserves_marks_and_tmdb(self):
        self.mark()
        self.store.save_tmdb("movies", "10", "CA", "test-fingerprint", {"status": "matched"})
        updated = copy.deepcopy(self.data)
        updated["movies"][0]["rating"] = "9"
        updated["movies"].append({"stream_id": 50, "name": "New movie"})
        self.store.sync(Mock(download=Mock(return_value=updated)))
        self.assertTrue(self.store.detail("movies", "10")["watched"])
        self.assertFalse(self.store.detail("movies", "50")["watched"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tmdb_metadata").fetchone()[0], 1)
        self.assertEqual(self.store.query("movies", watched="seen")["total"], 1)

    def test_filters_apply_before_count_and_pagination(self):
        data = snapshot()
        data["movies"] = [
            {"stream_id": number, "name": f"Movie {number:03}", "category_id": "1", "year": "2020"}
            for number in range(120)
        ]
        self.store.replace(data)
        for number in range(60):
            self.store.set_movie_watched(str(number), True)
        first = self.store.query("movies", watched="seen", page=1, category="1", q="Movie")
        second = self.store.query("movies", watched="seen", page=2)
        self.assertEqual((first["total"], first["pages"], len(first["items"])), (60, 2, 50))
        self.assertEqual(len(second["items"]), 10)
        self.assertTrue(all(item["watched"] for item in first["items"] + second["items"]))
        self.assertEqual(self.store.query("movies", watched="seen", genre="Drama")["total"], 0)
        self.assertEqual(self.store.query("movies", watched="unseen", q="Movie 119")["total"], 1)
        self.store.set_movie_watched("59", False)
        self.assertEqual(self.store.query("movies", watched="seen", page=999)["page"], 2)

    def test_initial_async_detail_and_person_buttons_have_csrf(self):
        self.data["movies"][0]["cast"] = "An Actor"
        self.store.replace(self.data)
        raw = self.store.detail("movies", "10")["data"]
        self.store.save_tmdb("movies", "10", "CA", fingerprint(raw), {
            "status": "matched", "cast": [{"name": "An Actor"}],
        })
        self.mark()
        for path in ("/movies?watched=seen", "/catalog/movies/detail?id=10&watched=seen",
                     "/people?name=An+Actor"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('class="watched-form', html)
            self.assertIn(f'name="csrf" value="{self.csrf}"', html)
            self.assertIn('aria-pressed="true"', html)
        payload = self.client.get("/api/catalog/movies?view=cards&watched=seen").json
        self.assertEqual(payload["total"], 1)
        self.assertTrue(payload["items"][0]["watched"])
        self.assertIn(f'name="csrf" value="{self.csrf}"', payload["html"])
        self.assertIn('watched=seen', payload["html"])
        html = self.client.get("/movies?watched=all").get_data(as_text=True)
        self.assertIn("Películas que no he visto", html)
        self.assertIn("Volver a ver", html)
        for path in ("/series", "/catalog/series/detail?id=20"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn('class="watched-form', html)
            self.assertNotIn('id="watched-seen"', html)

    def test_kids_and_foreign_movies_support_filter(self):
        self.data["vod_categories"].append({"category_id": "3", "category_name": "INFANTIL"})
        self.data["movies"].extend([
            {"stream_id": 40, "name": "Kids movie", "category_id": "3"},
            {"stream_id": 41, "name": "映画", "category_id": "1"},
        ])
        self.store.replace(self.data)
        raw = self.store.detail("movies", "40")["data"]
        self.store.save_tmdb("movies", "40", "CA", fingerprint(raw), {
            "status": "matched", "genres": ["Familia"],
        })
        self.mark("40")
        self.mark("41")
        for path, title in (("/kids/movies?watched=seen", "Kids movie"),
                            ("/movies?foreign=1&watched=seen", "映画")):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn(title, html)
            self.assertIn('id="watched-seen" value="seen" checked', html)

    def test_validation_csrf_and_no_get_mutation(self):
        self.assertEqual(self.mark(csrf="bad").status_code, 400)
        self.assertEqual(self.mark(watched="true").status_code, 400)
        self.assertEqual(self.mark("missing").status_code, 404)
        self.assertEqual(self.client.get("/catalog/movies/10/watched").status_code, 405)
        self.assertFalse(self.store.detail("movies", "10")["watched"])
        for path in ("/movies?watched=bad", "/api/catalog/movies?watched=bad",
                     "/series?watched=seen"):
            self.assertEqual(self.client.get(path).status_code, 400)
        with self.assertRaises(ValueError):
            self.store.query("series", watched="seen")
        with self.assertRaises(ValueError):
            self.store.set_movie_watched("10", "1")

    def test_watched_combines_with_subscription_filter(self):
        self.mark()
        raw = self.store.detail("movies", "10")["data"]
        self.store.save_tmdb("movies", "10", "CA", fingerprint(raw), {
            "status": "matched", "providers": {"flatrate": ["Netflix"]},
        })
        result = self.store.query(
            "movies", watched="seen", provider=["Netflix"], page=1,
        )
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["items"][0]["watched"])
        self.assertEqual(self.store.query(
            "movies", watched="unseen", provider=["Netflix"], page=1,
        )["total"], 0)

    def test_removing_last_item_from_page_clamps_pagination(self):
        self.data["movies"] = [
            {"stream_id": number, "name": f"Movie {number:03}"}
            for number in range(51)
        ]
        self.store.replace(self.data)
        path = "/api/catalog/movies?view=cards&watched=unseen&page=2"
        self.assertEqual(self.client.get(path).json["page"], 2)
        self.mark("50")
        payload = self.client.get(path).json
        self.assertEqual((payload["total"], payload["page"], payload["pages"]), (50, 1, 1))
        self.assertEqual(len(payload["items"]), 50)
        self.assertTrue(all(not item["watched"] for item in payload["items"]))

    def test_form_without_javascript_redirects_and_preserves_filter(self):
        html = self.client.get("/movies?watched=unseen&q=batman").get_data(as_text=True)
        action = unescape(re.search(r'class="watched-form[^"]*" method="post" action="([^"]+)"', html)[1])
        response = self.client.post(action, data={"csrf": self.csrf, "watched": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("watched=unseen", response.location)
        self.assertIn("q=batman", response.location)
        self.assertTrue(self.store.detail("movies", "10")["watched"])
