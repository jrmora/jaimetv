import tempfile
import unittest
from pathlib import Path

from app import create_app
from catalog import fingerprint
from test_app import snapshot


class MovieEraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        years = ["1950", "1979", "1980", "1999", "2000", "2014", "2015", "2026", "", "invalid", "0000"]
        self.data["movies"] = [
            {"stream_id": i, "name": f"Movie {i:03}", "year": year, "category_id": "1"}
            for i, year in enumerate(years)
        ]
        self.store.replace(self.data)
        self.client = self.app.test_client()

    def ids(self, **filters):
        return {item["id"] for item in self.store.query("movies", **filters)["items"]}

    def cache(self, record_id, date, status="matched", country="CA", identity=None):
        raw = self.store.detail("movies", record_id)["data"]
        self.store.save_tmdb("movies", record_id, country, identity or fingerprint(raw),
                             {"status": status, "release_date": date})

    def test_boundaries_union_unknown_and_default(self):
        for era, ids in (
            ("classic", {"0", "1"}), ("nostalgia", {"2", "3"}),
            ("modern", {"4", "5"}), ("recent", {"6", "7"}),
            ("unknown", {"8", "9", "10"}),
        ):
            self.assertEqual(self.ids(era=era), ids)
        self.assertEqual(self.ids(era=["recent", "modern"]), {"4", "5", "6", "7"})
        self.assertEqual(len(self.ids()), 11)
        self.assertEqual(self.ids(era=["recent", "recent"]), {"6", "7"})

    def test_tmdb_precedence_and_source_fallback(self):
        self.cache("0", "2020-01-01")
        self.cache("1", "2020-02-31")
        self.cache("2", "2020-01-01", status="candidates")
        self.cache("3", "2020-01-01", country="US")
        self.cache("4", "1985-01-01", identity="stale")
        self.assertEqual(self.ids(era="recent"), {"0", "6", "7"})
        self.assertEqual(self.ids(era="classic"), {"1"})
        self.assertEqual(self.ids(era="nostalgia"), {"2", "3"})
        self.assertEqual(self.ids(era="modern"), {"4", "5"})
        self.data["movies"][8]["releaseDate"] = "2001-05-01"
        self.store.replace(self.data)
        self.assertIn("8", self.ids(era="modern"))
        self.assertNotIn("8", self.ids(era="unknown"))
        self.assertEqual(self.store.detail("movies", "0")["data"]["year"], "1950")

    def test_initial_live_detail_back_and_validation(self):
        query = "era=recent&era=modern"
        html = self.client.get("/movies?" + query).get_data(as_text=True)
        self.assertIn('id="era-recent" value="recent" checked', html)
        self.assertIn('id="era-modern" value="modern" checked', html)
        payload = self.client.get("/api/catalog/movies?view=cards&" + query).json
        self.assertEqual(payload["total"], 4)
        self.assertIn("era=recent", payload["html"])
        self.assertIn("era=modern", payload["html"])
        detail = self.client.get("/catalog/movies/detail?id=6&" + query).get_data(as_text=True)
        self.assertIn("era=recent", detail)
        self.assertIn("era=modern", detail)
        for path in ("/kids/movies", "/movies?foreign=1"):
            self.assertIn('id="era-recent"', self.client.get(path).get_data(as_text=True))
        for path in ("/series", "/channels"):
            self.assertNotIn('id="era-recent"', self.client.get(path).get_data(as_text=True))
        for path in ("/movies?era=bad", "/api/catalog/movies?era=bad",
                     "/series?era=recent", "/channels?era=recent"):
            self.assertEqual(self.client.get(path).status_code, 400)
        with self.assertRaises(ValueError):
            self.store.query("series", era=["recent"])

    def test_combination_with_seen_and_global_pagination(self):
        self.data["movies"] = [
            {"stream_id": i, "name": f"Movie {i:03}", "year": "2020" if i < 55 else "1990"}
            for i in range(60)
        ]
        self.store.replace(self.data)
        payload = self.client.get("/api/catalog/movies?view=cards&era=recent&page=2").json
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (55, 2, 5))
        self.store.set_movie_watched("0", True)
        self.assertEqual(self.client.get("/api/catalog/movies?era=recent").json["total"], 54)
        payload = self.client.get("/api/catalog/movies?era=recent&watched=seen").json
        self.assertEqual([item["id"] for item in payload["items"]], ["0"])
        self.store.set_movie_banned("0", True)
        self.assertEqual(self.client.get("/api/catalog/movies?era=recent&watched=seen").json["total"], 0)
