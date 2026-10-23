import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from catalog import CatalogStore
from updater import write_state


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

    def test_coverage_only_appears_in_administration(self):
        for kind in ("movies", "series"):
            page = self.client.get(f"/admin?kind={kind}").get_data(as_text=True)
            self.assertIn("Cobertura TMDB", page)
            self.assertIn("títulos consultados en este catálogo (todos los filtros)", page)
        for path in ("/movies", "/series", "/kids/movies", "/kids/series", "/movies?foreign=1", "/series?foreign=1"):
            with patch.object(self.app.config["CATALOG_STORE"], "tmdb_coverage") as coverage:
                page = self.client.get(path).get_data(as_text=True)
            coverage.assert_not_called()
            self.assertNotIn("Cobertura TMDB", page)
            self.assertNotIn("títulos consultados", page)
            self.assertIn('id="tmdb-status"', page)

    def test_update_button_only_on_admin_when_configured(self):
        with patch.dict("os.environ", {"IPTV_UPDATE_STATE": str(Path(self.temp.name) / "state")}):
            self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.client = self.app.test_client()
        self.app.config["UPDATE_ENABLED"] = True
        page = self.client.get("/admin")
        self.assertIn(b'id="application-update"', page.data)
        self.assertNotIn(b'id="application-update"', self.client.get("/movies").data)

    def test_dates_are_persistent_and_separate_for_each_catalog(self):
        store = self.app.config["CATALOG_STORE"]
        with store.connect() as db:
            db.execute("INSERT INTO sync_metadata VALUES (1, ?)", ("2026-10-20T10:00:00+00:00",))
        store.save_provider_directory({"movies": ["Netflix"]})
        store.save_tmdb("movies", "1", "CA", "fingerprint", {
            "status": "matched", "checked_at": "2026-10-21T11:00:00+00:00",
        })
        store.save_tmdb("series", "2", "CA", "fingerprint", {
            "status": "missing", "checked_at": "2026-10-22T12:00:00+00:00",
        })
        reopened = CatalogStore(store.path)
        self.assertEqual(reopened.maintenance_dates("movies")["catalog"], "2026-10-20T10:00:00+00:00")
        self.assertIsNotNone(reopened.maintenance_dates("movies")["platforms"])
        self.assertIsNone(reopened.maintenance_dates("series")["platforms"])
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("2026-10-20 10:00:00 UTC", page)
        self.assertIn("2026-10-21 11:00:00 UTC", page)
        self.assertNotIn("2026-10-22 12:00:00 UTC", page)
        series = self.client.get("/admin?kind=series").get_data(as_text=True)
        self.assertIn("2026-10-22 12:00:00 UTC", series)
        self.assertIn("Sin registro de actualizaci", series)

    def test_empty_database_does_not_invent_dates(self):
        self.assertEqual(self.app.config["CATALOG_STORE"].maintenance_dates("movies"), {
            "catalog": None, "platforms": None, "tmdb": None,
        })
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertEqual(page.count("Sin registro de actualizaci"), 4)

    def test_application_keeps_last_success_after_failed_attempt(self):
        state = Path(self.temp.name) / "state"
        with patch.dict("os.environ", {"IPTV_UPDATE_STATE": str(state)}):
            app = create_app(Path(self.temp.name) / "catalog.db")
        app.config["UPDATE_ENABLED"] = True
        with patch("updater.time.time", return_value=1792756800):
            write_state(state, status="completed")
        write_state(state, status="running")
        write_state(state, status="failed")
        page = app.test_client().get("/admin").get_data(as_text=True)
        self.assertIn('datetime="2026-10-23T12:00:00+00:00"', page)
