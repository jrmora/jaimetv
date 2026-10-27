import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import create_app, main
from catalog import CatalogStore, fingerprint
from test_app import snapshot
from xtream import XtreamClient
from xtream import CatalogError


class VODOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.store = self.app.config["CATALOG_STORE"]
        self.store.replace(snapshot())
        self.client = self.app.test_client()

    def test_live_routes_removed_and_navigation_keeps_other_sections(self):
        for route in ("/channels", "/kids/channels", "/catalog/channels/detail?id=1",
                      "/api/catalog/channels", "/catalog/channels/1/watched"):
            self.assertEqual(self.client.get(route).status_code, 404)
        for route in ("/movies", "/series", "/kids/movies", "/admin"):
            page = self.client.get(route)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b"/channels", page.data)
            self.assertNotIn(b"Canales", page.data)
            self.assertIn(b"/kids/movies", page.data)
            if route == "/admin":
                self.assertIn(b"/movies?foreign=1", page.data)
                self.assertIn(b">Otros alfabetos</a>", page.data)
            else:
                self.assertNotIn(b"/movies?foreign=1", page.data)

    def test_download_only_requests_movies_and_series(self):
        client = XtreamClient("https://example.invalid", "user", "password")
        self.addCleanup(client.close)
        with patch.object(client, "fetch", return_value=[]) as fetch:
            downloaded = client.download()
        self.assertEqual(set(downloaded), {"vod_categories", "series_categories", "movies", "series"})
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            "get_vod_categories", "get_series_categories", "get_vod_streams", "get_series",
        ])

    def test_experimental_provider_details_are_removed_without_losing_catalog_state(self):
        raw = self.store.detail("movies", "10")["data"]
        metadata = {"status": "matched", "id": 123, "title": "Batman"}
        self.store.save_tmdb("movies", "10", "CA", fingerprint(raw), metadata)
        self.store.set_watchlisted("movies", "10", True)
        self.store.set_watched("movies", "10", True)
        self.store.set_kids_categories("movies", "10", {
            "superheroes": "include", "anime": "auto", "animation_hits": "auto", "family": "auto",
        })
        with self.store.connect() as db:
            db.execute("CREATE TABLE provider_metadata (kind TEXT, record_id TEXT, payload TEXT)")
            db.execute("INSERT INTO provider_metadata VALUES ('movies', '10', 'temporary diagnostic')")
        for _ in range(2):
            reopened = CatalogStore(self.path)
            with reopened.connect() as db:
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='provider_metadata'"
                ).fetchone())
            self.assertEqual(reopened.detail("movies", "10")["data"], raw)
            self.assertTrue(reopened.detail("movies", "10")["watchlisted"])
            self.assertTrue(reopened.detail("movies", "10")["watched"])
            self.assertEqual(reopened.tmdb_cached("movies", "10", "CA", fingerprint(raw)), metadata)
            self.assertEqual(reopened.kids_categories("movies", "10")[0]["mode"], "include")
        for kind, record_id in (("movies", "10"), ("series", "20")):
            html = self.client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertIn('id="tmdb-section"', html)
            self.assertIn('id="kids-classification"', html)
            self.assertNotIn('id="provider-info"', html)
            self.assertNotIn("provider-diagnostic", html)
            self.assertEqual(self.client.post(
                f"/catalog/{kind}/{record_id}/provider-info"
            ).status_code, 404)
        self.assertFalse(hasattr(XtreamClient, "fetch_info"))

    def test_sync_removes_legacy_live_tables_once_without_recreating_them(self):
        with self.store.connect() as db:
            db.execute("CREATE TABLE live_categories (category_id TEXT, raw_json TEXT)")
            db.execute("CREATE TABLE live_streams (stream_id TEXT, raw_json TEXT)")
            db.execute("INSERT INTO live_categories VALUES ('90', 'private legacy category')")
            db.execute("INSERT INTO live_streams VALUES ('1', 'private legacy stream')")
        data = snapshot()
        data["live_categories"] = None
        data["live_streams"] = None
        with self.store.connect() as db:
            db.execute("INSERT INTO sync_excluded_categories VALUES ('live_categories', '90')")
        CatalogStore(self.path)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_streams").fetchone()[0], 1)
        invalid = {**data, "series": None}
        with patch.object(XtreamClient, "download", return_value=invalid):
            client = XtreamClient("https://example.invalid", "user", "password")
            try:
                with self.assertRaises(CatalogError):
                    self.store.sync(client)
            finally:
                client.close()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_streams").fetchone()[0], 1)
        with patch.object(XtreamClient, "download", return_value=data):
            client = XtreamClient("https://example.invalid", "user", "password")
            try:
                self.store.sync(client)
            finally:
                client.close()
        reopened = CatalogStore(self.path)
        with reopened.connect() as db:
            self.assertEqual(db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('live_categories', 'live_streams')"
            ).fetchall(), [])
            self.assertEqual(db.execute(
                "SELECT * FROM sync_excluded_categories WHERE category_table='live_categories'"
            ).fetchall(), [])
        with patch.object(XtreamClient, "download", return_value=data):
            client = XtreamClient("https://example.invalid", "user", "password")
            try:
                reopened.sync(client)
            finally:
                client.close()
        with reopened.connect() as db:
            self.assertEqual(db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('live_categories', 'live_streams')"
            ).fetchall(), [])
        self.assertEqual(set(reopened.stats()["totals"]), {"movies", "series"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.path), "stats"]), 0)
            self.assertEqual(main(["--db", str(self.path), "search", "Batman"]), 0)
        self.assertNotIn("Canales", output.getvalue())
        self.assertNotIn("Batman TV", output.getvalue())
