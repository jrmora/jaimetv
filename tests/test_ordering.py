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
                        self.assertIn(f'value="{column}:{direction}" selected', page)
                        self.assertNotIn('id="sort"', page)

    def test_invalid_combined_order_is_rejected(self):
        for value in ("name", "name:invalid", "invalid:asc", ":desc"):
            self.assertEqual(self.client.get("/movies", query_string={"ordering": value}).status_code, 400)
