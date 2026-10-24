import copy
import tempfile
import unittest
from pathlib import Path

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot


class MovieVersionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"] = [
            {"stream_id": 10, "name": "The Same Movie (2020)", "category_id": "1",
             "stream_icon": "https://example.invalid/en.jpg"},
            {"stream_id": 11, "name": "La misma película 4K (2020)", "category_id": "2",
             "stream_icon": "https://example.invalid/es.jpg"},
            {"stream_id": 12, "name": "Unmatched"},
        ]
        self.store.replace(self.data)
        self.cache("10", 100)
        self.cache("11", 100)
        self.client = self.app.test_client()

    def cache(self, record_id, tmdb_id, kind="movies", **changes):
        raw = self.store.detail(kind, record_id)["data"]
        self.store.save_tmdb(kind, record_id, "CA", fingerprint(raw), {
            "status": "matched", "id": tmdb_id, "title": "La misma película",
            "genres": ["Drama"], "rating": 8, "votes": 200,
            "release_date": "2020-01-01", **changes,
        })

    def test_one_card_spanish_title_badge_and_unique_stats(self):
        result = self.store.query("movies", page=1)
        self.assertEqual(result["total"], 2)
        grouped = next(item for item in result["items"] if item["id"] == "10")
        self.assertEqual(grouped["name"], "La misma película")
        self.assertEqual(grouped["version_count"], 2)
        self.assertEqual(grouped["poster_url"], "https://example.invalid/en.jpg")
        self.assertEqual(self.store.stats()["totals"]["movies"], 2)
        self.assertEqual(self.store.movie_title_count(), 2)
        admin = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Películas únicas: 2", admin)
        page = self.client.get("/movies?children=all&watched=all").get_data(as_text=True)
        self.assertEqual(page.count('<article class="catalog-card'), 2)
        self.assertIn("2 versiones", page)
        self.assertIn("La misma película", page)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movies").fetchone()[0], 3)

    def test_any_version_matches_search_filters_and_shared_flags(self):
        for filters in ({"q": "The Same Movie"}, {"q": "4K"}, {"q": "misma"},
                        {"genre": ["Drama"]}, {"era": ["recent"]}):
            result = self.store.query("movies", page=1, **filters)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["version_count"], 2)
        self.store.set_movie_watched("11", True)
        self.store.set_watchlisted("movies", "10", True)
        for record_id in ("10", "11"):
            record = self.store.detail("movies", record_id)
            self.assertTrue(record["watched"])
            self.assertTrue(record["watchlisted"])
        self.assertEqual(self.store.query("movies", watched="seen", page=1)["total"], 1)
        self.assertEqual(self.store.query("movies", watched="unseen", page=1)["total"], 1)
        self.assertEqual(self.store.query("movies", watched="unseen", q="same", page=1)["total"], 1)
        self.assertEqual(self.store.query("movies", watchlist=True, page=1)["total"], 1)
        self.store.set_movie_banned("11", True)
        self.assertEqual(self.store.query("movies", q="same", page=1)["total"], 0)
        self.assertEqual(self.store.query("movies", banned="only", page=1)["total"], 1)
        self.store.set_movie_banned("10", False)
        self.store.set_movie_watched("10", False)
        self.store.set_watchlisted("movies", "11", False)
        for record_id in ("10", "11"):
            record = self.store.detail("movies", record_id)
            self.assertFalse(record["watched"])
            self.assertFalse(record["banned"])
            self.assertFalse(record["watchlisted"])

    def test_existing_marks_and_new_versions_survive_sync_and_restart(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO watched_movies VALUES ('11', 'legacy')")
            db.execute("INSERT INTO banned_movies VALUES ('11', 'legacy')")
            db.execute("INSERT INTO watchlist VALUES ('movies', '11', 'legacy')")
        changed = copy.deepcopy(self.data)
        changed["movies"].append({"stream_id": 14, "name": "Another copy"})
        self.store.replace(changed)
        self.cache("14", 100)
        reopened = CatalogStore(self.path)
        self.assertEqual(reopened.query("movies", banned="only", page=1)["total"], 1)
        detail = reopened.detail("movies", "14")
        self.assertEqual(detail["version_count"], 3)
        for key in ("watched", "banned", "watchlisted"):
            self.assertTrue(detail[key])
        reopened.set_movie_watched("14", False)
        reopened.set_movie_banned("14", False)
        reopened.set_watchlisted("movies", "14", False)
        self.assertEqual(reopened.query("movies", watched="seen", page=1)["total"], 0)
        self.assertEqual(reopened.query("movies", watchlist=True, page=1)["total"], 0)

    def test_detail_shows_both_posters_and_preserves_raw_records(self):
        for record_id in ("10", "11"):
            page = self.client.get(f"/catalog/movies/detail?id={record_id}").get_data(as_text=True)
            self.assertIn("2 versiones disponibles", page)
            self.assertIn('class="version-posters"', page)
            self.assertIn("https://example.invalid/en.jpg", page)
            self.assertIn("https://example.invalid/es.jpg", page)
            self.assertIn("<h1 class=\"text-break mb-0\">La misma película</h1>", page)
        self.assertEqual(self.store.detail("movies", "10")["data"], self.data["movies"][0])
        self.assertEqual(self.store.detail("movies", "11")["data"], self.data["movies"][1])

    def test_only_current_confirmed_positive_integer_ids_group(self):
        for changes in ({"status": "candidates"}, {"status": "missing"},
                        {"id": None}, {"id": "100"}, {"id": 0}, {"id": True}):
            self.cache("11", 100, **changes)
            self.assertEqual(self.store.query("movies", page=1)["total"], 3, changes)
        self.cache("11", 100)
        changed = copy.deepcopy(self.data)
        changed["movies"][1]["name"] = "Different identity"
        self.store.replace(changed)
        self.assertEqual(self.store.query("movies", page=1)["total"], 3)
        self.assertEqual(self.store.detail("movies", "10")["version_count"], 1)
        self.assertEqual(self.store.stats()["totals"]["movies"], 3)

    def test_grouping_precedes_pagination_and_does_not_group_series(self):
        data = snapshot()
        data["movies"] = [{"stream_id": n, "name": f"Movie {n:03}"} for n in range(104)]
        data["series"].append({"series_id": 21, "name": "Series copy"})
        self.store.replace(data)
        for n in range(104):
            self.cache(str(n), n // 2 + 1, title=f"Película {n // 2:03}")
        for record_id in ("20", "21"):
            self.cache(record_id, 100, kind="series")
        first = self.store.query("movies", page=1)
        second = self.store.query("movies", page=2)
        self.assertEqual((first["total"], first["pages"], len(first["items"])), (52, 2, 50))
        self.assertEqual(len(second["items"]), 2)
        self.assertEqual(len({item["name"] for item in first["items"] + second["items"]}), 52)
        self.assertEqual(self.store.query("series", page=1)["total"], 2)
        self.assertEqual(self.store.stats()["totals"], {"movies": 52, "series": 2})

    def test_same_poster_without_same_tmdb_identity_is_not_enough(self):
        self.cache("11", 200)
        self.assertEqual(self.store.query("movies", page=1)["total"], 3)
        self.store.set_movie_watched("10", True)
        self.assertFalse(self.store.detail("movies", "11")["watched"])

    def test_weighted_mean_counts_each_movie_once(self):
        self.cache("12", 200, title="Otra película", rating=9, votes=50)
        before = self.store.query("movies", sort_by="rating", sort="desc", page=1)
        changed = copy.deepcopy(self.data)
        changed["movies"].extend({"stream_id": n, "name": f"Copy {n}"} for n in range(20, 30))
        self.store.replace(changed)
        for n in range(20, 30):
            self.cache(str(n), 100)
        after = self.store.query("movies", sort_by="rating", sort="desc", page=1)
        self.assertEqual([i["id"] for i in before["items"]], [i["id"] for i in after["items"]])
        self.assertEqual(after["total"], 2)

    def test_recommendations_use_group_title_badge_and_shared_watchlist(self):
        self.store.set_watchlisted("movies", "11", True)
        result = self.store.recommended_titles("movies", 999, [100])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "La misma película")
        self.assertEqual(result[0]["version_count"], 2)
        self.assertTrue(result[0]["watchlisted"])
