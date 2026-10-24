import tempfile
import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot
from xtream import CatalogError


class WebSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.testing = True
        self.client = self.app.test_client()
        self.provider = Mock()
        self.provider.download.return_value = snapshot()
        self.app.config["XTREAM_CLIENT_FACTORY"] = Mock(return_value=self.provider)
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.token = session["sync_csrf"]
        self.threads = []
        real_thread = threading.Thread

        def make_thread(**kwargs):
            thread = real_thread(**kwargs)
            self.threads.append(thread)
            return thread

        self.patcher = patch("app.threading.Thread", side_effect=make_thread)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.join_threads)

    def join_threads(self):
        for thread in self.threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def start(self):
        return self.client.post("/api/sync", data={"csrf": self.token})

    def test_button_csrf_and_no_store(self):
        page = self.client.get("/admin")
        self.assertIn(b'id="catalog-sync"', page.data)
        self.assertIn(b"sync.js", page.data)
        for token in ("", "invalid", "\u00e1"):
            self.assertEqual(self.client.post("/api/sync", data={"csrf": token}).status_code, 400)
        self.app.config["XTREAM_CLIENT_FACTORY"].assert_not_called()
        state = self.client.get("/api/sync")
        self.assertEqual(state.json["status"], "idle")
        self.assertEqual(state.headers["Cache-Control"], "no-store")

    def test_background_sync_refreshes_cards_without_server_restart(self):
        self.assertEqual(self.start().status_code, 202)
        self.join_threads()
        result = self.client.get("/api/sync").json
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["totals"]["movies"], 4)
        self.assertEqual(result["changes"]["movies"]["new"], 4)
        self.assertEqual(result["duplicates"], {})
        self.provider.close.assert_called_once()
        self.assertIn(b"4 resultados", self.client.get("/movies").data)
        self.assertEqual(self.start().status_code, 202)
        self.join_threads()
        result = self.client.get("/api/sync").json
        self.assertEqual(result["changes"]["movies"]["new"], 0)
        self.assertEqual(result["changes"]["movies"]["unchanged"], 4)

    def test_duplicate_start_rejected_and_navigation_remains_available(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.provider.download.side_effect = lambda: (release.wait(5), snapshot())[1]
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.client.get("/api/sync").json["status"], "running")
        self.assertEqual(self.start().status_code, 409)
        other = self.app.test_client()
        self.assertEqual(other.get("/api/sync").json["status"], "running")
        self.assertEqual(self.client.get("/movies").status_code, 200)
        release.set()
        self.join_threads()
        self.assertEqual(self.client.get("/api/sync").json["status"], "completed")
        self.provider.download.assert_called_once()

    def test_failure_preserves_catalog_and_allows_retry(self):
        store = self.app.config["CATALOG_STORE"]
        store.replace(snapshot())
        before = store.stats()
        self.provider.download.side_effect = CatalogError("API no disponible")
        self.start()
        self.join_threads()
        state = self.client.get("/api/sync").json
        self.assertEqual(state, {"status": "failed", "error": "API no disponible"})
        self.assertEqual(store.stats(), before)
        self.provider.close.assert_called_once()
        self.provider.download.side_effect = None
        self.assertEqual(self.start().status_code, 202)
        self.join_threads()
        self.assertEqual(self.client.get("/api/sync").json["status"], "completed")

    def test_unexpected_error_is_visible_without_exposing_credentials(self):
        self.provider.download.side_effect = RuntimeError("private-provider-password")
        self.start()
        self.join_threads()
        response = self.client.get("/api/sync")
        self.assertEqual(response.json["status"], "failed")
        self.assertNotIn(b"private-provider-password", response.data)
        self.provider.close.assert_called_once()

    def test_sync_button_preserves_all_tmdb_results_and_platforms_across_restart(self):
        store = self.app.config["CATALOG_STORE"]
        data = snapshot()
        store.replace(data)
        for kind, record_id, status in (
            ("movies", "10", "matched"), ("movies", "11", "candidates"),
            ("movies", "12", "missing"), ("series", "20", "matched"),
        ):
            raw = store.detail(kind, record_id)["data"]
            store.save_tmdb(kind, record_id, "CA", fingerprint(raw), {
                "status": status, "id": 63, "checked_at": "2026-10-23T12:00:00+00:00",
                "genres": ["Drama"], "providers": {"flatrate": ["Netflix"]},
                "cast": [{"name": "An Actor"}], "directors": ["A Director"],
                "candidates": [{"id": 64, "title": "Alternative"}],
            })
        store.save_provider_directory({"movies": ["Netflix"], "series": ["Crave"]})
        store.set_movie_watched("10", True)
        store.set_movie_banned("11", True)
        tables = ("tmdb_metadata", "tmdb_provider_directory", "maintenance_updates",
                  "watched_movies", "banned_movies")

        def saved_rows(database):
            with database.connect() as db:
                return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                        for table in tables}

        before = saved_rows(store)
        changed = copy.deepcopy(data)
        changed["movies"][0].update(rating="9", category_id="2", stream_icon="https://example.invalid/new.jpg")
        changed["movies"].append({"stream_id": 100, "name": "New movie"})
        changed["series"][0]["last_modified"] = "1234567890"
        self.provider.download.return_value = changed
        self.assertEqual(self.start().status_code, 202)
        self.join_threads()
        self.assertEqual(self.client.get("/api/sync").json["status"], "completed")
        reopened = CatalogStore(store.path)
        self.assertEqual(saved_rows(reopened), before)
        self.assertEqual(reopened.tmdb_coverage("movies")["checked"], 3)
        self.assertEqual(reopened.tmdb_coverage("series")["checked"], 1)
        self.assertEqual(reopened.tmdb_pending("movies", 500), ["13", "100"])

        changed["movies"][0]["name"] = "A genuinely different movie"
        self.provider.download.return_value = changed
        self.assertEqual(self.start().status_code, 202)
        self.join_threads()
        self.assertEqual(saved_rows(reopened), before)
        self.assertIn("10", reopened.tmdb_pending("movies", 500))


if __name__ == "__main__":
    unittest.main()
