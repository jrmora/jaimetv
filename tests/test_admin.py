import tempfile
import unittest
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from unittest.mock import patch

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot
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
            for control in ("catalog-sync", "tmdb-batch-start",
                            "tmdb-batch-cancel", "tmdb-kind", "tmdb-batch-limit"):
                self.assertIn(f'id="{control}"'.encode(), response.data)
            self.assertIn(b"admin.js", response.data)
            self.assertNotIn(b'id="tmdb-directory"', response.data)
        for path in ("/movies", "/series", "/kids/movies"):
            response = self.client.get(path)
            self.assertIn(b'href="/admin"', response.data)
            for control in ("catalog-sync", "tmdb-directory", "tmdb-batch-start"):
                self.assertNotIn(f'id="{control}"'.encode(), response.data)
        self.assertEqual(self.client.get("/admin?kind=channels").status_code, 400)

    def test_coverage_only_appears_in_administration(self):
        for kind in ("movies", "series"):
            page = self.client.get(f"/admin?kind={kind}").get_data(as_text=True)
            self.assertIn("Cobertura TMDB", page)
            self.assertIn('data-coverage-kind="movies"', page)
            self.assertIn('data-coverage-kind="series"', page)
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
        self.assertIn("2026-10-20 06:00:00 EDT · Montreal", page)
        self.assertIn("2026-10-21 07:00:00 EDT · Montreal", page)
        self.assertNotIn("2026-10-22 08:00:00 EDT · Montreal", page)
        series = self.client.get("/admin?kind=series").get_data(as_text=True)
        self.assertIn("2026-10-22 08:00:00 EDT · Montreal", series)
        self.assertIn("Sin registro de actualizaci", series)

    def test_montreal_dates_follow_daylight_saving_and_preserve_original_timestamp(self):
        store = self.app.config["CATALOG_STORE"]
        for timestamp, expected in (
            ("2026-01-15T02:00:00+00:00", "2026-01-14 21:00:00 EST"),
            ("2026-07-15T02:00:00+00:00", "2026-07-14 22:00:00 EDT"),
        ):
            with self.subTest(timestamp=timestamp):
                with store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO sync_metadata VALUES (1, ?)", (timestamp,))
                page = self.client.get("/admin").get_data(as_text=True)
                self.assertIn(f"{expected} · Montreal", page)
                self.assertIn(f'datetime="{timestamp}"', page)
                self.assertEqual(store.maintenance_dates("movies")["catalog"], timestamp)

    def test_empty_database_does_not_invent_dates(self):
        self.assertEqual(self.app.config["CATALOG_STORE"].maintenance_dates("movies"), {
            "catalog": None, "platforms": None, "tmdb": None,
        })
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertEqual(page.count("Sin registro de actualizaci"), 4)
        self.assertEqual(page.count('aria-valuenow="0"'), 2)

    def test_blocked_movies_access_is_in_administration(self):
        store = self.app.config["CATALOG_STORE"]
        store.replace(snapshot())
        store.set_movie_banned("10", True)
        page = self.client.get("/admin").get_data(as_text=True)
        link = re.search(r'<a [^>]*href="([^"]+)"[^>]*>Ver películas bloqueadas</a>', page)
        self.assertIsNotNone(link)
        url = unescape(link.group(1))
        self.assertEqual(parse_qs(urlsplit(url).query), {
            "banned": ["only"], "children": ["all"], "watched": ["all"],
        })
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn("Películas bloqueadas", html)
        self.assertIn('aria-label="Desbloquear"', html)
        self.assertNotIn('id="banned-filter"', html)

    def test_coverage_percentage_counts_all_consulted_statuses(self):
        store = self.app.config["CATALOG_STORE"]
        store.replace(snapshot())
        for record_id, status in (("10", "matched"), ("11", "candidates"), ("12", "missing")):
            raw = store.detail("movies", record_id)["data"]
            store.save_tmdb("movies", record_id, "CA", fingerprint(raw), {"status": status})
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertIn('aria-valuenow="75.0"', page)
        self.assertIn("75.0 %", page)
        self.assertIn('value="75.0" max="100"', page)
        self.assertNotIn('style="width:', page)

    def test_coverage_separates_movies_and_series_regardless_of_selected_tab(self):
        store = self.app.config["CATALOG_STORE"]
        store.replace(snapshot())
        for kind in ("movies", "series"):
            page = self.client.get(f"/admin?kind={kind}").get_data(as_text=True)
            for catalog_kind, label in (("movies", "Películas"), ("series", "Series")):
                link = re.search(rf'<th scope="row"><a href="([^"]+)">{label}</a></th>', page)
                self.assertIsNotNone(link)
                url = unescape(link.group(1))
                self.assertEqual(urlsplit(url).path, f"/{catalog_kind}")
                self.assertEqual(parse_qs(urlsplit(url).query), {"children": ["all"], "watched": ["all"]})
                self.assertEqual(self.client.get(url).status_code, 200)
            page = re.sub(r'<th scope="row"><a [^>]*>([^<]+)</a></th>', r'<th scope="row">\1</th>', page)
            page = re.sub(r'<a [^>]*>(\d+)</a>', r'\1', page)
            self.assertIn(
                '<tr data-coverage-kind="movies"><th scope="row">Películas</th>'
                '<td>4</td><td>0</td><td>0</td><td>0</td><td>0</td><td>4</td>',
                page,
            )
            self.assertIn(
                '<tr data-coverage-kind="series"><th scope="row">Series</th>'
                '<td>1</td><td>0</td><td>0</td><td>0</td><td>0</td><td>1</td>',
                page,
            )

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

    def test_candidate_counts_link_to_unrestricted_candidate_listing(self):
        store = self.app.config["CATALOG_STORE"]
        store.replace(snapshot())
        for kind, record_id in (("movies", "10"), ("series", "20")):
            raw = store.detail(kind, record_id)["data"]
            store.save_tmdb(kind, record_id, "CA", fingerprint(raw), {"status": "candidates"})
        store.set_movie_watched("10", True)
        page = self.client.get("/admin").get_data(as_text=True)
        for kind in ("movies", "series"):
            row = re.search(rf'<tr data-coverage-kind="{kind}">(.*?)</tr>', page, re.S).group(1)
            link = unescape(re.search(r'<td><a href="([^"]+)"', row).group(1))
            params = parse_qs(urlsplit(link).query)
            self.assertEqual(params["tmdb_status"], ["candidates"])
            self.assertEqual(params["children"], ["all"])
            self.assertEqual(params["watched"], ["all"])
            response = self.client.get(link)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Batman", response.data)
