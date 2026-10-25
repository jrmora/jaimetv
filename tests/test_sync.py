import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from catalog import CatalogStore, excluded_category
from test_app import snapshot
from tmdb import COUNTRY, fingerprint
from xtream import ACTIONS, CatalogError


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CatalogStore(Path(self.temp.name) / "catalog.db")
        self.data = snapshot()
        self.store.replace(self.data)

    def sync(self, data):
        client = Mock()
        client.download.return_value = data
        return self.store.sync(client)

    def test_inserts_updates_skips_identical_and_preserves_tmdb_across_reopen(self):
        original = self.data["movies"][0]
        payload = {"status": "matched", "id": 63}
        self.store.save_tmdb("movies", "10", COUNTRY, fingerprint(original), payload)
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["rating"] = "9.5"
        changed["movies"][1] = dict(reversed(list(changed["movies"][1].items())))
        changed["movies"].append({"stream_id": 100, "name": "New", "category_id": "1"})
        self.sync(changed)
        report = self.store.sync_report["movies"]
        self.assertEqual(report["new"], 1)
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["unchanged"], 3)
        reopened = CatalogStore(self.store.path)
        self.assertEqual(reopened.tmdb_cached("movies", "10", COUNTRY, fingerprint(original)), payload)
        self.assertEqual(reopened.detail("movies", "10")["data"]["rating"], "9.5")
        self.sync({table: [] for table in ACTIONS})
        self.assertEqual(self.store.query("movies")["total"], 5)
        self.assertFalse(any(item["unavailable"] for item in self.store.query("movies")["items"]))
        self.assertEqual(self.store.tmdb_cached("movies", "10", COUNTRY, fingerprint(original)), payload)

    def test_marks_titles_withdrawn_by_provider_and_restores_them(self):
        from app import create_app

        self.store.set_movie_watched("11", True)
        changed = copy.deepcopy(self.data)
        changed["movies"] = [row for row in changed["movies"] if row["stream_id"] != 11]
        changed["series"] = []
        self.sync(changed)
        self.assertEqual(self.store.sync_report["movies"]["unavailable"], 1)
        self.assertEqual(self.store.sync_report["series"]["unavailable"], 0)
        items = {item["id"]: item["unavailable"] for item in self.store.query("movies", watched="all")["items"]}
        self.assertTrue(items.pop("11"))
        self.assertFalse(any(items.values()))
        self.assertTrue(self.store.detail("movies", "11")["unavailable"])
        self.assertTrue(self.store.detail("movies", "11")["watched"])
        self.assertFalse(self.store.detail("movies", "10")["unavailable"])
        client = create_app(self.store.path).test_client()
        page = client.get("/movies?watched=all&children=all").get_data(as_text=True)
        self.assertEqual(page.count("is-unavailable"), 1)
        self.assertIn("Ya no disponible", page)
        self.assertIn("El proveedor ya no ofrece", client.get("/catalog/movies/detail?id=11").get_data(as_text=True))

        self.sync(changed)
        self.assertEqual(self.store.sync_report["movies"]["unavailable"], 0)
        self.sync(self.data)
        self.assertEqual(self.store.sync_report["movies"]["restored"], 1)
        self.assertFalse(self.store.detail("movies", "11")["unavailable"])
        self.assertTrue(self.store.detail("movies", "11")["watched"])

    def test_summary_lists_changed_titles_with_fields_and_persists(self):
        from app import create_app

        self.assertIsNone(self.store.last_sync_summary())
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["rating"] = "9.5"
        changed["movies"] = [row for row in changed["movies"] if row["stream_id"] != 11]
        changed["movies"].append({"stream_id": 100, "name": "Nueva", "category_id": "1"})
        self.sync(changed)
        summary = CatalogStore(self.store.path).last_sync_summary()
        movies = summary["changes"]["movies"]
        self.assertEqual(movies["new"], [{"id": "100", "name": "Nueva"}])
        self.assertEqual(movies["updated"], [{"id": "10", "name": "Batman", "fields": ["rating"]}])
        self.assertEqual(movies["unavailable"], [{"id": "11", "name": "ÁRBOL"}])
        self.assertEqual(summary["report"]["movies"]["updated"], 1)
        page = create_app(self.store.path).test_client().get("/admin").get_data(as_text=True)
        self.assertIn("Cambios de la última sincronización", page)
        self.assertIn("Actualizados: 1", page)
        self.assertIn('href="/catalog/movies/detail?id=10">Batman</a>', page)
        self.assertIn("— valoración", page)
        self.assertIn("Ya no disponibles: 1", page)
        self.sync(self.data)
        movies = self.store.last_sync_summary()["changes"]["movies"]
        self.assertEqual(movies["restored"], [{"id": "11", "name": "ÁRBOL"}])
        self.assertEqual(movies["new"], [])

    def test_excludes_categories_primary_secondary_and_existing_records(self):
        changed = copy.deepcopy(self.data)
        for table in ("vod_categories", "series_categories"):
            changed[table].extend([
                {"category_id": "90", "category_name": "ADULTOS (XXX)"},
                {"category_id": "91", "category_name": "S TERROR Y SUSPENSO"},
                {"category_id": "92", "category_name": "CAST. HORROR"},
            ])
        changed["movies"][0]["category_ids"] = ["1", 90]
        changed["movies"][1]["category_id"] = "91"
        changed["movies"].append({"stream_id": 99, "name": "Forbidden", "category_id": "92"})
        changed["series"][0]["category_ids"] = [91]
        self.sync(changed)
        self.assertIsNone(self.store.detail("movies", "10"))
        self.assertIsNone(self.store.detail("movies", "11"))
        self.assertIsNone(self.store.detail("movies", "99"))
        self.assertIsNone(self.store.detail("series", "20"))
        self.assertEqual(self.store.query("movies")["total"], 2)
        for kind in ("movies", "series"):
            self.assertEqual(len(self.store.categories(kind)), 2)
        self.assertEqual(self.store.sync_report["movies"]["excluded"], 3)
        self.assertEqual(self.store.sync_report["movies"]["removed_excluded"], 2)

    def test_saved_blocked_category_is_used_when_missing_from_download(self):
        data = copy.deepcopy(self.data)
        data["vod_categories"].append({"category_id": "90", "category_name": "TERROR/SUSPENSO"})
        data["movies"][0]["category_ids"] = [90]
        self.store.replace(data)
        incoming = copy.deepcopy(self.data)
        incoming["movies"] = [{"stream_id": 77, "name": "No", "category_ids": ["90"]}]
        self.sync(incoming)
        self.assertIsNone(self.store.detail("movies", "10"))
        self.assertIsNone(self.store.detail("movies", "77"))
        self.assertEqual(self.store.query("movies")["total"], 3)
        self.assertEqual(len(self.store.categories("movies")), 2)
        # Even after category removal, an incomplete later download stays excluded.
        self.sync(incoming)
        self.assertIsNone(self.store.detail("movies", "77"))

    def test_invalid_data_rolls_back_updates_exclusions_and_tmdb(self):
        self.store.save_tmdb("movies", "10", COUNTRY, fingerprint(self.data["movies"][0]), {"id": 63})
        before = self.store.stats()
        for invalid in (None, [False], [{"series_id": 20, "name": float("nan")}]):
            changed = copy.deepcopy(self.data)
            changed["vod_categories"][0]["category_name"] = "HORROR"
            changed["movies"][1]["name"] = "Changed"
            changed["series"] = invalid
            with self.assertRaises(CatalogError):
                self.sync(changed)
            self.assertEqual(self.store.stats(), before)
            self.assertEqual(self.store.detail("movies", "10")["data"], self.data["movies"][0])
            self.assertEqual(self.store.tmdb_cached("movies", "10", COUNTRY, fingerprint(self.data["movies"][0])), {"id": 63})

    def test_exclusion_names_are_not_confused_with_other_categories(self):
        for name in ("ADULTOS (XXX)", "XXX", "TERROR/SUSPENSO", "CAST. TERROR", "S TERROR Y SUSPENSO", "Horror", "Térror"):
            self.assertTrue(excluded_category(name), name)
        for name in ("MISTERIO", "THRILLER", "SCI-FI", "INFANTIL", "ADVENTURE", "SUSPENSO"):
            self.assertFalse(excluded_category(name), name)


if __name__ == "__main__":
    unittest.main()
