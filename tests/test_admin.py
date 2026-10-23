import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.client = self.app.test_client()

    def test_controls_are_centralized_and_both_catalogs_supported(self):
        for kind in ("movies", "series"):
            response = self.client.get(f"/admin?kind={kind}")
            self.assertEqual(response.status_code, 200)
            for control in ("catalog-sync", "tmdb-directory", "tmdb-batch-start",
                            "tmdb-batch-cancel", "tmdb-kind", "tmdb-batch-limit"):
                self.assertIn(f'id="{control}"'.encode(), response.data)
            self.assertIn(b"admin.js", response.data)
        for path in ("/movies", "/series", "/channels", "/kids/movies"):
            response = self.client.get(path)
            self.assertIn(b'href="/admin"', response.data)
            for control in ("catalog-sync", "tmdb-directory", "tmdb-batch-start"):
                self.assertNotIn(f'id="{control}"'.encode(), response.data)
        self.assertEqual(self.client.get("/admin?kind=channels").status_code, 400)

    def test_update_button_only_on_admin_when_configured(self):
        with patch.dict("os.environ", {"IPTV_UPDATE_STATE": str(Path(self.temp.name) / "state")}):
            self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.client = self.app.test_client()
        self.app.config["UPDATE_ENABLED"] = True
        page = self.client.get("/admin")
        self.assertIn(b'id="application-update"', page.data)
        self.assertNotIn(b'id="application-update"', self.client.get("/movies").data)
