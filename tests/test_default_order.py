import tempfile
import unittest
from pathlib import Path

from app import create_app
from test_app import snapshot
from catalog import fingerprint


class DefaultOrderTests(unittest.TestCase):
    def test_only_valid_current_tmdb_scores_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "test.db")
            store = app.config["CATALOG_STORE"]
            data = snapshot()
            data["movies"] = [{"stream_id": i, "name": f"Movie {i}", "rating": 10} for i in range(9)]
            store.replace(data)
            for i, payload in enumerate([
                {"status": "matched", "rating": 7.2, "votes": 100},
                {"status": "matched", "rating": 10, "votes": 0},
                {"status": "candidates", "rating": 10, "votes": 100},
                {"status": "matched", "rating": "10", "votes": 100},
                {"status": "matched", "rating": 11, "votes": 100},
                {"status": "matched", "rating": 10, "votes": 100},
                {"status": "matched", "rating": 10, "votes": 100},
                {"status": "matched", "rating": 0, "votes": 1},
            ]):
                raw = store.detail("movies", str(i))["data"]
                store.save_tmdb("movies", str(i), "US" if i == 5 else "CA",
                                "stale" if i == 6 else fingerprint(raw), payload)
            client = app.test_client()
            payload = client.get("/api/catalog/movies?view=cards").json
            scores = {item["id"]: item["rating"] for item in payload["items"]}
            self.assertEqual(scores["0"], 7.2)
            self.assertEqual(scores["7"], 0)
            self.assertTrue(all(scores[str(i)] is None for i in (1, 2, 3, 4, 5, 6, 8)))
            self.assertEqual([item["id"] for item in payload["items"][:2]], ["0", "7"])
            self.assertIn("TMDB: 7.2/10", payload["html"])
            self.assertNotIn("TMDB: 10", payload["html"])
            self.assertEqual(store.detail("movies", "0")["data"]["rating"], 10)

    def test_rating_then_year_descending_before_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "test.db")
            data = snapshot()
            records = [
                {"name": "Older", "rating": 8, "year": "1990"},
                {"name": "Recent", "rating": 8, "year": "2025"},
                {"name": "Highest", "rating": 9, "year": "1980"},
                {"name": "No year", "rating": 8},
                {"name": "No rating", "year": "2026"},
            ]
            data["movies"] = [dict(row, stream_id=i, cast="Test Actor") for i, row in enumerate(records)]
            data["series"] = [dict(row, series_id=i) for i, row in enumerate(records)]
            app.config["CATALOG_STORE"].replace(data)
            store = app.config["CATALOG_STORE"]
            for kind in ("movies", "series"):
                for i, row in enumerate(records):
                    if "rating" in row:
                        raw = store.detail(kind, str(i))["data"]
                        store.save_tmdb(kind, str(i), "CA", fingerprint(raw), {
                            "status": "matched", "rating": row["rating"], "votes": 100,
                        })
            client = app.test_client()
            expected = ["Highest", "Recent", "Older", "No year", "No rating"]
            for kind in ("movies", "series"):
                for query in ("", "?view=cards"):
                    payload = client.get(f"/api/catalog/{kind}{query}").json
                    self.assertEqual([item["name"] for item in payload["items"]], expected)
                html = client.get(f"/{kind}").get_data(as_text=True)
                self.assertIn('value="rating" selected', html)
                self.assertIn('value="desc" selected', html)
            payload = client.get("/api/catalog/movies?sort_by=name&sort=asc").json
            self.assertEqual([item["name"] for item in payload["items"]], sorted(expected))
            html = client.get("/people?name=Test+Actor").get_data(as_text=True)
            self.assertLess(html.index("Ver ficha de Highest"), html.index("Ver ficha de Recent"))
