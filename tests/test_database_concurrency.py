import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from catalog import CatalogStore
from test_app import snapshot


class DatabaseConcurrencyTests(unittest.TestCase):
    def test_catalog_reader_does_not_block_preference_or_tmdb_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            store = CatalogStore(path)
            store.replace(snapshot())
            failures = []
            completed = threading.Event()

            def write():
                try:
                    store.set_movie_watched("10", True)
                    store.set_movie_banned("11", True)
                    store.set_watchlisted("series", "20", True)
                    store.save_recommendations("movies", 63, [12], 100)
                    store.save_tmdb("movies", "10", "CA", "test", {"status": "matched"})
                except Exception as exc:
                    failures.append(exc)
                finally:
                    completed.set()

            reader = sqlite3.connect(path)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM movies").fetchall()
                worker = threading.Thread(target=write)
                worker.start()
                finished_while_reading = completed.wait(3)
            finally:
                reader.close()
            worker.join(timeout=35)
            self.assertTrue(finished_while_reading, "Una lectura del catálogo bloqueó las escrituras.")
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            reopened = CatalogStore(path)
            self.assertTrue(reopened.detail("movies", "10")["watched"])
            self.assertTrue(reopened.detail("movies", "11")["banned"])
            self.assertTrue(reopened.detail("series", "20")["watchlisted"])
            self.assertEqual(reopened.cached_recommendations("movies", 63, 0), [12])

    def test_existing_database_is_migrated_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            store = CatalogStore(path)
            store.replace(snapshot())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
            reopened = CatalogStore(path)
            self.assertEqual(reopened.query("movies")["total"], 4)
            with reopened.connect() as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
