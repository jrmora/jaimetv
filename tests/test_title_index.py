import copy
import tempfile
import unittest
from pathlib import Path

from catalog import CatalogStore, fingerprint
from test_app import snapshot


class TitleIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CatalogStore(Path(self.temp.name) / "catalog.db")
        self.data = snapshot()
        self.store.replace(self.data)

    def cache(self, record_id="10", kind="movies", **changes):
        raw = self.store.detail(kind, record_id)["data"]
        payload = {
            "status": "matched", "id": 500 + int(record_id), "title": "Ñandú Veloz",
            "original_title": "Fast Rhea", "cast": [{"name": "Zoë Actriz"}], "directors": ["Directora Única"],
            "rating": 8, "votes": 10, "release_date": "2001-05-06", **changes,
        }
        self.store.save_tmdb(kind, record_id, "CA", fingerprint(raw), payload)

    def ids(self, q, kind="movies"):
        return [item["id"] for item in self.store.query(kind, q=q)["items"]]

    def index_row(self, record_id, kind="movies"):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM title_index WHERE kind=? AND record_id=?", (kind, record_id)).fetchone()
        return dict(row) if row else None

    def test_saved_tmdb_metadata_is_indexed_in_the_same_write(self):
        self.cache()
        row = self.index_row("10")
        self.assertEqual((row["matched"], row["tmdb_id"], row["rating"], row["votes"], row["year"]), (1, 510, 8, 10, 2001))
        for query in ("ñandú", "ÑANDÚ VELOZ", "fast rhea", "zoë", "directora"):
            self.assertEqual(self.ids(query), ["10"], query)
        self.assertEqual(self.store.query("movies", actor="Zoë Actriz")["total"], 1)
        self.assertEqual(self.ids("100%_"), ["12"])
        self.assertEqual(self.ids("1_0"), [])

    def test_unconfirmed_or_stale_metadata_is_not_searchable(self):
        self.cache(status="candidates")
        self.assertEqual(self.index_row("10")["matched"], 0)
        self.assertEqual(self.ids("ñandú"), [])
        self.cache()
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "Nombre Nuevo"
        self.store.replace(changed)
        row = self.index_row("10")
        self.assertEqual((row["matched"], row["tmdb_id"]), (0, None))
        self.assertEqual(self.ids("ñandú"), [])
        self.assertEqual(self.ids("nombre nuevo"), ["10"])

    def test_removed_titles_leave_no_index_rows_and_missing_rows_are_rebuilt(self):
        changed = copy.deepcopy(self.data)
        removed = changed["movies"].pop()
        self.store.replace(changed)
        self.assertIsNone(self.index_row(str(removed["stream_id"])))
        self.cache(record_id="20", kind="series")
        with self.store.connect() as db:
            db.execute("DELETE FROM title_index")
        self.assertEqual(self.ids("ñandú", "series"), ["20"])
        self.assertEqual(self.index_row("20", "series")["matched"], 1)
        self.ids("cualquiera")
        with self.store.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM title_index").fetchone()[0]
        self.assertEqual(total, len(changed["movies"]) + len(changed["series"]))


if __name__ == "__main__":
    unittest.main()
