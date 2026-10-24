import tempfile
import unittest
from pathlib import Path

from app import create_app
from catalog import CatalogStore
from test_app import snapshot


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.store = self.app.config["CATALOG_STORE"]
        self.store.replace(snapshot())
        self.client = self.app.test_client()
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def save(self, kind, record_id, value="1", csrf=None):
        return self.client.post(
            f"/catalog/{kind}/{record_id}/watchlist",
            data={"watchlist": value, "csrf": self.csrf if csrf is None else csrf},
            headers={"Accept": "application/json"},
        )

    def test_movies_series_persistence_sync_and_removal(self):
        for kind, record_id in (("movies", "10"), ("series", "20")):
            self.assertEqual(self.save(kind, record_id).json, {"id": record_id, "watchlist": True})
            self.save(kind, record_id)
            reopened = CatalogStore(self.path)
            reopened.replace(snapshot())
            self.assertTrue(reopened.detail(kind, record_id)["watchlisted"])
            result = reopened.query(kind, watchlist=True)
            self.assertEqual([item["id"] for item in result["items"]], [record_id])
            payload = self.client.get(f"/api/catalog/{kind}?watchlist=1&children=all&watched=all&view=cards").json
            self.assertEqual(payload["total"], 1)
            self.assertIn('aria-label="Quitar de Mi lista"', payload["html"])
            self.assertEqual(self.save(kind, record_id, "0").status_code, 200)
            self.assertEqual(reopened.query(kind, watchlist=True)["total"], 0)

    def test_controls_details_and_validation(self):
        for kind, record_id in (("movies", "10"), ("series", "20")):
            html = self.client.get(f"/{kind}?watchlist=1&children=all&watched=all").get_data(as_text=True)
            self.assertIn('name="watchlist" value="1"', html)
            self.assertIn("Mi lista", html)
            html = self.client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertIn('aria-label="Quiero ver"', html)
        self.assertEqual(self.save("movies", "10", csrf="bad").status_code, 400)
        self.assertEqual(self.save("movies", "10", "invalid").status_code, 400)
        self.assertEqual(self.save("movies", "missing").status_code, 404)
        self.assertEqual(self.save("channels", "10").status_code, 404)
        self.assertEqual(self.client.get("/movies?watchlist=invalid").status_code, 400)

    def test_saving_does_not_change_watched_or_banned_and_filters_apply(self):
        self.save("movies", "10")
        detail = self.store.detail("movies", "10")
        self.assertFalse(detail["watched"])
        self.assertFalse(detail["banned"])
        self.assertEqual(self.store.query("movies", watchlist=True, q="absent")["total"], 0)
        self.store.set_movie_banned("10", True)
        self.assertEqual(self.store.query("movies", watchlist=True)["total"], 0)
        self.assertTrue(self.store.detail("movies", "10")["watchlisted"])
