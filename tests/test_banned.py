import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot


class BannedMoviesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"][0]["cast"] = "An Actor"
        self.store.replace(self.data)
        self.client = self.app.test_client()
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def ban(self, value="1", record_id="10", csrf=None):
        return self.client.post(
            f"/catalog/movies/{record_id}/banned",
            data={"banned": value, "csrf": self.csrf if csrf is None else csrf},
            headers={"Accept": "application/json"},
        )

    def test_hide_persist_sync_and_unban_without_losing_metadata(self):
        raw = self.store.detail("movies", "10")["data"]
        self.store.set_movie_watched("10", True)
        self.store.save_tmdb("movies", "10", "CA", fingerprint(raw), {"status": "matched"})
        self.assertEqual(self.ban().json, {"id": "10", "banned": True})
        self.assertEqual(self.ban().status_code, 200)
        reopened = CatalogStore(self.path)
        self.assertTrue(reopened.detail("movies", "10")["banned"])
        self.assertEqual(reopened.query("movies", q="Batman")["total"], 0)
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["rating"] = 9
        changed["movies"].append({"stream_id": 50, "name": "New"})
        reopened.sync(Mock(download=Mock(return_value=changed)))
        self.assertEqual(reopened.query("movies", banned="only")["total"], 1)
        self.assertFalse(reopened.detail("movies", "50")["banned"])
        self.assertTrue(reopened.detail("movies", "10")["watched"])
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tmdb_metadata").fetchone()[0], 1)
        self.assertEqual(self.ban("0").status_code, 200)
        self.assertEqual(reopened.query("movies", q="Batman")["total"], 1)
        self.assertEqual(reopened.query("movies", banned="only")["total"], 0)

    def test_initial_async_people_and_detail_controls(self):
        raw = self.store.detail("movies", "10")["data"]
        self.store.save_tmdb("movies", "10", "CA", fingerprint(raw), {
            "status": "matched", "cast": [{"name": "An Actor"}],
        })
        for path in ("/movies", "/people?name=An+Actor"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('class="banned-form', html)
            self.assertIn("No me interesa", html)
            self.assertNotIn('id="banned-filter"', html)
            self.assertNotIn("Películas bloqueadas", html)
        payload = self.client.get("/api/catalog/movies?view=cards").json
        self.assertIn('class="banned-form', payload["html"])
        html = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn("No me interesa", html)
        self.assertIn('class="banned-form', html)
        self.assertIn(f'name="csrf" value="{self.csrf}"', html)
        self.ban()
        for path in ("/movies?q=Batman", "/people?name=An+Actor"):
            self.assertNotIn("No me interesa", self.client.get(path).get_data(as_text=True))
        payload = self.client.get("/api/catalog/movies?view=cards&q=Batman").json
        self.assertEqual(payload["total"], 0)
        html = self.client.get("/movies?banned=only").get_data(as_text=True)
        self.assertNotIn('id="banned-filter"', html)
        self.assertIn('type="hidden" name="banned" value="only"', html)
        self.assertIn('class="banned-form', html)
        self.assertIn("Desbloquear", html)
        html = self.client.get("/catalog/movies/detail?id=10&banned=only").get_data(as_text=True)
        self.assertIn("Desbloquear", html)
        self.assertIn(f'name="csrf" value="{self.csrf}"', html)
        payload = self.client.get("/api/catalog/movies?view=cards&banned=only").json
        self.assertEqual(payload["total"], 1)
        self.assertTrue(payload["items"][0]["banned"])
        self.assertIn('class="banned-form', payload["html"])
        self.assertIn("banned=only", payload["html"])
        self.assertEqual(self.client.get("/api/catalog/movies?q=Batman").json["total"], 0)
        for path in ("/series", "/catalog/series/detail?id=20"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('class="banned-form', html)
            self.assertIn("/catalog/series/20/banned", html)
            self.assertNotIn('id="banned-filter"', html)

    def test_combined_filters_and_pagination_count_exclude_banned(self):
        self.data["movies"] = [
            {"stream_id": number, "name": f"Movie {number:03}", "category_id": "1"}
            for number in range(51)
        ]
        self.store.replace(self.data)
        self.ban(record_id="50")
        payload = self.client.get("/api/catalog/movies?view=cards&page=2").json
        self.assertEqual((payload["total"], payload["pages"], payload["page"]), (50, 1, 1))
        self.assertEqual(len(payload["items"]), 50)
        self.assertTrue(all(not item["banned"] for item in payload["items"]))
        self.assertEqual(self.store.query("movies", banned="only", genre="Drama")["total"], 0)
        self.store.set_movie_watched("50", True)
        self.assertEqual(self.store.query("movies", banned="only", watched="seen")["total"], 1)
        self.assertEqual(self.store.query("movies", watched="seen")["total"], 0)
        self.assertEqual(self.store.query("movies", banned="only", watched="unseen")["total"], 0)

    def test_kids_foreign_and_series_separation(self):
        self.data["vod_categories"].append({"category_id": "3", "category_name": "INFANTIL"})
        self.data["movies"] += [
            {"stream_id": 40, "name": "Kids movie", "category_id": "3"},
            {"stream_id": 41, "name": "映画", "category_id": "1"},
        ]
        self.store.replace(self.data)
        raw = self.store.detail("movies", "40")["data"]
        self.store.save_tmdb("movies", "40", "CA", fingerprint(raw), {
            "status": "matched", "genres": ["Familia"],
        })
        self.ban(record_id="40")
        self.ban(record_id="41")
        for path, title in (("/kids/movies", "Kids movie"), ("/movies?foreign=1", "映画")):
            self.assertNotIn(title, self.client.get(path).get_data(as_text=True))
            url = path + ("&" if "?" in path else "?") + "banned=only"
            self.assertIn(title, self.client.get(url).get_data(as_text=True))
        self.assertEqual(self.store.query("series")["total"], 1)

    def test_invalid_state_csrf_missing_record_and_get_do_not_modify(self):
        self.assertEqual(self.ban(csrf="bad").status_code, 400)
        self.assertEqual(self.ban("true").status_code, 400)
        self.assertEqual(self.ban(record_id="missing").status_code, 404)
        self.assertEqual(self.client.get("/catalog/movies/10/banned").status_code, 405)
        self.assertFalse(self.store.detail("movies", "10")["banned"])
        for path in ("/movies?banned=all", "/series?banned=all"):
            self.assertEqual(self.client.get(path).status_code, 400)
        with self.assertRaises(ValueError):
            self.store.set_movie_banned("10", "1")
        with self.assertRaises(ValueError):
            self.store.query("series", banned="all")
        response = self.client.post("/catalog/movies/10/banned?banned=only",
                                    data={"csrf": self.csrf, "banned": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("banned=only", response.location)

    def test_series_blocking_persists_and_is_separate_from_movies(self):
        self.store.set_watchlisted("series", "20", True)
        raw = self.store.detail("series", "20")["data"]
        self.store.save_tmdb("series", "20", "CA", fingerprint(raw), {
            "status": "matched", "id": 100, "cast": [{"name": "An Actor"}],
        })
        url = "/catalog/series/20/banned"
        response = self.client.post(url, data={"banned": "1", "csrf": self.csrf},
                                    headers={"Accept": "application/json"})
        self.assertEqual(response.json, {"id": "20", "banned": True})
        reopened = CatalogStore(self.path)
        self.assertTrue(reopened.detail("series", "20")["banned"])
        self.assertTrue(reopened.detail("series", "20")["watchlisted"])
        self.assertEqual(reopened.query("series", q="Batman")["total"], 0)
        self.assertEqual(reopened.recommended_titles("series", 101, [100]), [])
        self.assertEqual(self.client.get("/api/catalog/series?watchlist=1").json["total"], 0)
        page = self.client.get("/people?name=An+Actor").get_data(as_text=True)
        self.assertNotIn("Batman: la serie", page)
        payload = self.client.get("/api/catalog/series?view=cards&banned=only").json
        self.assertEqual(payload["total"], 1)
        self.assertTrue(payload["items"][0]["banned"])
        self.assertIn("Desbloquear", payload["html"])
        self.assertIn('name="banned" value="only"',
                      self.client.get("/series?banned=only").get_data(as_text=True))
        self.assertIn("Ver series bloqueadas", self.client.get("/admin").get_data(as_text=True))
        self.assertFalse(self.store.detail("movies", "10")["banned"])
        reopened.sync(Mock(download=Mock(return_value=self.data)))
        self.assertTrue(reopened.detail("series", "20")["banned"])
        response = self.client.post(url + "?banned=only", data={"banned": "0", "csrf": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertIn("/catalog/series/detail?", response.location)
        self.assertEqual(reopened.query("series")["total"], 1)
        self.assertEqual(reopened.query("series", banned="only")["total"], 0)
        self.assertEqual(len(reopened.recommended_titles("series", 101, [100])), 1)

    def test_series_block_validation_and_missing_id(self):
        for path, data, status in (
            ("/catalog/series/20/banned", {"csrf": "bad", "banned": "1"}, 400),
            ("/catalog/series/20/banned", {"csrf": self.csrf, "banned": "true"}, 400),
            ("/catalog/series/missing/banned", {"csrf": self.csrf, "banned": "1"}, 404),
            ("/catalog/unknown/20/banned", {"csrf": self.csrf, "banned": "1"}, 404),
        ):
            self.assertEqual(self.client.post(path, data=data).status_code, status)
        self.assertEqual(self.client.get("/catalog/series/20/banned").status_code, 405)
        self.assertFalse(self.store.detail("series", "20")["banned"])
        with self.assertRaises(ValueError):
            self.store.set_banned("series", "20", "1")

    def test_series_pagination_kids_and_overlapping_movie_ids(self):
        self.data["series"] = [
            {"series_id": number, "name": f"Series {number:03}", "category_id": "1"}
            for number in range(51)
        ]
        self.store.replace(self.data)
        raw = self.store.detail("series", "10")["data"]
        self.store.save_tmdb("series", "10", "CA", fingerprint(raw), {
            "status": "matched", "genres": ["Familia"],
        })
        self.assertTrue(self.store.set_banned("series", "10", True))
        self.assertFalse(self.store.detail("movies", "10")["banned"])
        payload = self.client.get("/api/catalog/series?view=cards&page=2&children=all").json
        self.assertEqual((payload["total"], payload["pages"], payload["page"]), (50, 1, 1))
        self.assertTrue(all(not item["banned"] for item in payload["items"]))
        self.assertNotIn("Series 010", self.client.get("/kids/series").get_data(as_text=True))
        self.assertIn("Series 010", self.client.get("/kids/series?banned=only").get_data(as_text=True))
