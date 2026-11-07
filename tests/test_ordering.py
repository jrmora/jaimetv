import tempfile
import unittest
from pathlib import Path

from app import create_app
from test_app import snapshot


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.config["CATALOG_STORE"].replace(snapshot())
        self.client = self.app.test_client()

    def test_combined_order_matches_legacy_parameters(self):
        for kind in ("movies", "series"):
            for column in ("name", "category_name", "year", "rating"):
                for direction in ("asc", "desc"):
                    with self.subTest(kind=kind, column=column, direction=direction):
                        base = f"/api/catalog/{kind}?view=cards&children=all"
                        combined = self.client.get(f"{base}&ordering={column}:{direction}")
                        legacy = self.client.get(f"{base}&sort_by={column}&sort={direction}")
                        self.assertEqual(combined.status_code, 200)
                        self.assertEqual(combined.json["items"], legacy.json["items"])
                        page = self.client.get(f"/{kind}?ordering={column}:{direction}").get_data(as_text=True)
                        self.assertIn(f'id="ordering" value="{column}:{direction}"', page)
                        self.assertNotIn('id="sort"', page)

    def test_invalid_combined_order_is_rejected(self):
        for value in ("name", "name:invalid", "invalid:asc", ":desc"):
            self.assertEqual(self.client.get("/movies", query_string={"ordering": value}).status_code, 400)

    def test_votes_option_only_offers_most_voted_first(self):
        for path in ("/movies", "/series", "/kids/movies", "/kids/series"):
            with self.subTest(path=path):
                page = self.client.get(f"{path}?ordering=votes:desc")
                self.assertEqual(page.status_code, 200)
                html = page.get_data(as_text=True)
                self.assertIn('id="ordering" value="votes:desc"', html)
                self.assertIn('data-ordering="votes:desc" aria-pressed="true"', html)
                self.assertIn("Más votadas primero", html)
                self.assertNotIn('value="votes:asc"', html)
        combined = self.client.get("/api/catalog/movies?ordering=votes:desc")
        legacy = self.client.get("/api/catalog/movies?sort_by=votes&sort=desc")
        self.assertEqual(combined.status_code, 200)
        self.assertEqual(combined.json["items"], legacy.json["items"])
