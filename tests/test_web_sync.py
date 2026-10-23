import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
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


if __name__ == "__main__":
    unittest.main()
