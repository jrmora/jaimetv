import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import create_app, main
from catalog import CatalogStore
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
