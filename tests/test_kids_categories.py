import copy
import json
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app import create_app
from catalog import CatalogStore, fingerprint
from kids_categories import KIDS_CATEGORIES, automatic_kids_categories
from test_app import snapshot


class AutomaticKidsCategoriesTests(unittest.TestCase):
    def classify(self, name, **metadata):
        return json.loads(automatic_kids_categories(name, json.dumps({"status": "matched", **metadata})))

    def test_requested_examples_and_overlaps(self):
        for name in ("Avengers: Endgame", "Batman", "Spider-Man: No Way Home"):
            self.assertIn("superheroes", self.classify(name, genres=["Acción"]))
        for name in ("El rey león", "La era de hielo", "Ice Age"):
            self.assertEqual(self.classify(name, genres=["Animación", "Familia"]), ["animation_hits"])
        self.assertEqual(self.classify("La guardería de papá", genres=["Comedia"]), ["family"])
        self.assertEqual(self.classify("Daddy Day Care", genres=["Comedy"]), ["family"])
        self.assertEqual(self.classify("Big Hero 6", genres=["Animación", "Familia"],
                                       companies=["Walt Disney Animation Studios"]),
                         ["superheroes", "animation_hits"])

    def test_asian_animation_requires_animation_not_just_language(self):
        for language in ("ja", "ko", "zh", "cn"):
            self.assertEqual(self.classify("Una animación", genres=["Animation"], original_language=language),
                             ["anime"])
            self.assertEqual(self.classify("Drama", genres=["Drama"], original_language=language), [])
        self.assertEqual(self.classify("Un anime", genres=["Animación"], keywords=["anime"]), ["anime"])
        self.assertEqual(self.classify("Una animación", genres=["Animation"],
                                       countries=["Japan"], original_language="en"), ["anime"])
        self.assertEqual(self.classify("Una animación", genres=["Animación"],
                                       companies=["Studio Ghibli", "Walt Disney Pictures"]), ["anime"])

    def test_studios_genres_keywords_and_no_speculative_synopsis_matching(self):
        self.assertEqual(self.classify("Una peli", genres=["Animación", "Familia"], companies=["Pixar"]), ["animation_hits"])
        self.assertEqual(self.classify("Una familia", genres=["Familia", "Comedia"]), ["family"])
        self.assertEqual(self.classify("Un héroe", genres=["Drama"], keywords=["superhero"]), ["superheroes"])
        self.assertEqual(self.classify("Un drama", genres=["Drama"], companies=["Pixar"]), [])
        self.assertEqual(self.classify("El abogado del diablo", genres=["Drama"],
                                       overview="Hablan de Batman y el anime."), [])
        self.assertEqual(self.classify("Carrie", genres=["Terror"], keywords=["super power"]), [])
        self.assertEqual(automatic_kids_categories("Batman", None), "[]")
        self.assertEqual(automatic_kids_categories("Batman", '{"status": "candidates"}'), "[]")

    def test_family_animation_does_not_require_famous_studio_or_box_office_success(self):
        self.assertEqual(KIDS_CATEGORIES["animation_hits"], "Cine animado")
        for genres in (["Animación", "Familia"], ["Animation", "Family"]):
            self.assertEqual(self.classify("Una aventura desconocida", genres=genres,
                                           companies=["Estudio independiente"], votes=2, rating=4),
                             ["animation_hits"])
        self.assertEqual(self.classify("Animación adulta", genres=["Animación", "Comedia"],
                                       companies=["Sony Pictures Animation"]), [])
        self.assertEqual(self.classify("Anime familiar", genres=["Animación", "Familia"],
                                       original_language="ja"), ["anime"])


class KidsCategoriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"] = [
            {"stream_id": 10, "name": "Avengers", "category_id": "1"},
            {"stream_id": 11, "name": "Anime"},
            {"stream_id": 12, "name": "El rey león"},
            {"stream_id": 13, "name": "La guardería de papá"},
            {"stream_id": 14, "name": "Otra película"},
            {"stream_id": 15, "name": "Familia"},
        ]
        self.store.replace(self.data)
        self.client = self.app.test_client()
        self.cache("10", title="Avengers", genres=["Acción"], keywords=["superhero"])
        self.cache("11", title="Anime", genres=["Animación"], original_language="ja")
        self.cache("12", title="El rey león", genres=["Animación", "Familia"], companies=["Walt Disney Pictures"])
        self.cache("13", title="La guardería de papá", genres=["Comedia"])
        self.cache("14", title="Otra película", genres=["Drama"])
        self.cache("15", title="Familia", genres=["Familia"])
        self.cache("20", kind="series", title="Batman: la serie", genres=["Animación"], keywords=["superhero"])

    def cache(self, record_id, kind="movies", **changes):
        raw = self.store.detail(kind, record_id)["data"]
        self.store.save_tmdb(kind, record_id, "CA", fingerprint(raw), {
            "status": "matched", "id": int(record_id), "title": "Título",
            "genres": [], **changes,
        })

    def ids(self, kind="movies", **filters):
        return {row["id"] for row in self.store.query(kind, children="only", **filters)["items"]}

    def modes(self, **changes):
        return {key: changes.get(key, "auto") for key in KIDS_CATEGORIES}

    def test_expanded_kids_scope_and_category_filters_for_both_kinds(self):
        self.assertEqual(self.ids(), {"10", "12", "13", "15"})
        for category, expected in (
            ("superheroes", {"10"}), ("anime", {"11"}),
            ("animation_hits", {"12"}), ("family", {"13", "15"}),
        ):
            self.assertEqual(self.ids(kids_category=category), expected)
        self.assertEqual(self.ids(kind="series", kids_category="superheroes"), {"20"})
        self.assertEqual(self.ids(kind="series", kids_category="anime"), set())
        # The main catalog's existing exclusion switch keeps its original meaning.
        self.assertIn("10", {row["id"] for row in self.store.query("movies", children="exclude")["items"]})

    def test_multiple_categories_use_or_and_default_to_three_without_anime(self):
        self.assertEqual(self.ids(kids_category=["superheroes", "family"]), {"10", "13", "15"})
        self.assertEqual(self.ids(kids_category=["anime", "family"]), {"11", "13", "15"})
        self.store.set_kids_categories("movies", "12", self.modes(superheroes="include"))
        combined = self.store.query("movies", children="only",
                                    kids_category=["superheroes", "animation_hits"], page=1)
        self.assertEqual(combined["total"], 2)
        self.assertEqual({row["id"] for row in combined["items"]}, {"10", "12"})
        default = self.client.get("/kids/movies").get_data(as_text=True)
        controls = re.findall(r'<input[^>]*name="kids_category"[^>]*>', default)
        self.assertEqual(len(controls), 4)
        self.assertTrue(all('type="checkbox"' in control for control in controls))
        self.assertEqual(sum("checked" in control for control in controls), 3)
        self.assertNotIn("checked", next(control for control in controls if 'value="anime"' in control))
        self.assertNotIn('id="kids-all"', default)
        url = "/api/catalog/movies?view=cards&children=only&kids_category=anime&kids_category=family"
        data = self.client.get(url).json
        self.assertEqual(data["total"], 3)
        self.assertIn("kids_category=anime", data["html"])
        self.assertIn("kids_category=family", data["html"])
        detail = self.client.get(
            "/catalog/movies/detail?id=11&children=only&kids_category=anime&kids_category=family"
        ).get_data(as_text=True)
        self.assertIn("kids_category=anime", detail)
        self.assertIn("kids_category=family", detail)
        with self.assertRaises(ValueError):
            self.store.query("movies", children="only", kids_category=[])

    def test_categories_combine_with_filters_and_facets_and_keep_blocks(self):
        self.assertEqual(self.ids(kids_category="superheroes", genre="Drama"), set())
        result = self.store.query("movies", children="only", kids_category="superheroes",
                                  genre="Drama", include_genres=True, page=1)
        self.assertEqual(result["available_genres"], ["Acción"])
        self.assertEqual(result["selected_genres"], [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.ids(kids_category="anime", q="no existe"), set())
        self.store.set_watched("movies", "10", True)
        self.assertEqual(self.ids(kids_category="superheroes", watched="unseen"), set())
        self.store.set_banned("movies", "10", True)
        self.assertEqual(self.ids(kids_category="superheroes"), set())
        self.assertEqual(self.ids(kids_category="superheroes", banned="only"), {"10"})
        self.assertEqual(self.store.query("movies", children="only", kids_category="superheroes",
                                          include_genres=True)["available_genres"], [])

    def test_anime_is_exclusive_in_all_normal_queries_and_recommendations(self):
        self.store.set_watchlisted("movies", "11", True)
        for filters in (
            {}, {"children": "all"}, {"children": "only"}, {"children": "exclude"},
            {"q": "Anime"}, {"genre": "Animación"}, {"watchlist": True},
            {"foreign": 1}, {"banned": "only"},
        ):
            result = self.store.query("movies", **filters)
            self.assertNotIn("11", {row["id"] for row in result["items"]}, filters)
        self.assertEqual(self.ids(kids_category="anime"), {"11"})
        self.assertFalse(self.store.detail("movies", "11")["banned"])
        self.assertNotIn("11", {row["id"] for row in self.store.recommended_titles("movies", 10, [11, 12])})
        self.store.set_kids_categories("movies", "11", self.modes(family="include", superheroes="include"))
        self.assertNotIn("11", self.ids(kids_category="family"))
        self.assertNotIn("11", self.ids(kids_category="superheroes"))
        self.assertIn("11", self.ids(kids_category="anime"))
        self.store.set_kids_categories("movies", "11", self.modes(anime="exclude", family="include"))
        self.assertIn("11", self.ids(kids_category="family"))
        self.assertNotIn("11", self.ids(kids_category="anime"))
        self.assertIn("11", {row["id"] for row in self.store.recommended_titles("movies", 10, [11, 12])})

    def test_exclusive_anime_covers_versions_series_facets_and_live_action_stays_visible(self):
        self.cache("14", id=11, genres=["Animación"], title="Otra versión")
        self.cache("20", kind="series", genres=["Animación", "Ciencia ficción"], original_language="ja")
        self.cache("15", genres=["Drama"], original_language="ko", countries=["South Korea"])
        self.assertEqual(self.store.query("series", children="all")["total"], 0)
        result = self.store.query("series", children="all", include_genres=True)
        self.assertEqual(result["available_genres"], [])
        self.assertEqual(self.ids(kind="series", kids_category="anime"), {"20"})
        self.assertNotIn("14", {row["id"] for row in self.store.query("movies")["items"]})
        self.assertIn("15", {row["id"] for row in self.store.query("movies")["items"]})
        self.assertEqual(self.store.recommended_titles("series", 1, [20]), [])

    def test_web_search_watchlist_people_and_genres_do_not_leak_anime(self):
        self.cache("11", title="Dragon Ball", genres=["Animación", "Ciencia ficción"],
                   original_language="ja", cast=[{"name": "Actor exclusivo"}])
        self.store.set_watchlisted("movies", "11", True)
        for url in (
            "/api/catalog/movies?children=all&q=Dragon%20Ball",
            "/api/catalog/movies?children=only&genre=Ciencia%20ficci%C3%B3n",
            "/api/catalog/movies?children=all&watchlist=1",
            "/api/catalog/movies?children=all&actor=Actor%20exclusivo",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response.json["total"], 0, url)
        response = self.client.get("/api/catalog/movies?children=only&kids_category=anime&q=Dragon%20Ball")
        self.assertEqual([item["id"] for item in response.json["items"]], ["11"])
        facets = self.client.get("/api/catalog/movies?view=cards&children=all").json
        self.assertNotIn("Ciencia ficción", facets["available_genres"])
        people = self.client.get("/people?name=Actor%20exclusivo").get_data(as_text=True)
        self.assertNotIn('<article class="catalog-card', people)
        self.assertEqual(self.store.query("movies", person="Actor exclusivo")["total"], 0)
        self.store.set_banned("movies", "11", True)
        self.assertEqual(self.ids(kids_category="anime"), set())
        self.assertTrue(self.store.detail("movies", "11")["banned"])

    def test_pagination_counts_unique_titles_before_limiting(self):
        data = copy.deepcopy(self.data)
        data["series"] = [{"series_id": 100 + n, "name": f"Serie {n:03}"} for n in range(61)]
        self.store.replace(data)
        for n in range(61):
            self.cache(str(100 + n), kind="series", title=f"Serie {n:03}",
                       genres=["Animación"], original_language="ja")
        pages = [self.store.query("series", children="only", kids_category="anime", page=n)
                 for n in (1, 2, 99)]
        self.assertEqual([result["total"] for result in pages], [61, 61, 61])
        self.assertEqual([len(result["items"]) for result in pages], [50, 11, 11])
        self.assertEqual([result["page"] for result in pages], [1, 2, 2])
        first = {row["id"] for row in pages[0]["items"]}
        second = {row["id"] for row in pages[1]["items"]}
        self.assertFalse(first & second)

    def test_override_auto_reset_and_persistence_after_sync_and_restart(self):
        self.assertTrue(self.store.set_kids_categories("movies", "14", self.modes(family="include")))
        self.assertIn("14", self.ids(kids_category="family"))
        self.store.set_kids_categories("movies", "10", self.modes(superheroes="exclude", family="include"))
        self.assertNotIn("10", self.ids(kids_category="superheroes"))
        self.assertIn("10", self.ids(kids_category="family"))
        self.store.replace(self.data)
        reopened = CatalogStore(self.path)
        settings = {item["id"]: item for item in reopened.kids_categories("movies", "10")}
        self.assertEqual(settings["superheroes"]["mode"], "exclude")
        self.assertTrue(settings["family"]["enabled"])
        self.assertEqual(self.store.detail("movies", "14")["data"], self.data["movies"][4])
        self.store.set_kids_categories("movies", "10", self.modes())
        self.assertIn("10", self.ids(kids_category="superheroes"))
        self.assertNotIn("10", self.ids(kids_category="family"))

    def test_overrides_are_shared_by_versions_including_new_ones_not_across_kinds(self):
        self.cache("14", id=10, title="Avengers", genres=["Acción"])
        self.store.set_kids_categories("movies", "14", self.modes(superheroes="exclude", family="include"))
        self.assertEqual(self.ids(kids_category="superheroes"), set())
        self.assertEqual(self.store.kids_categories("movies", "10"), self.store.kids_categories("movies", "14"))
        self.cache("15", id=10, title="Avengers", genres=["Acción"])
        self.assertEqual(self.store.kids_categories("movies", "15"), self.store.kids_categories("movies", "14"))
        self.cache("20", kind="series", id=10, keywords=["superhero"])
        self.assertEqual(self.ids(kind="series", kids_category="superheroes"), {"20"})
        self.store.set_kids_categories("movies", "15", self.modes())
        self.assertEqual(self.ids(kids_category="superheroes"), {"10"})

    def test_stale_automatic_metadata_is_removed_and_manual_overrides_remain(self):
        self.store.set_kids_categories("movies", "10", self.modes(family="include"))
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "Otro nombre"
        self.store.replace(changed)
        self.assertNotIn("10", self.ids(kids_category="superheroes"))
        self.assertIn("10", self.ids(kids_category="family"))
        self.cache("10", title="Otro nombre", genres=["Animación"], original_language="ja")
        self.assertIn("10", self.ids(kids_category="anime"))
        self.assertNotIn("10", self.ids(kids_category="family"))
        self.assertEqual(next(item for item in self.store.kids_categories("movies", "10")
                              if item["id"] == "family")["mode"], "include")

    def test_unmatched_titles_can_be_assigned_without_tmdb_and_index_is_reused(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM tmdb_metadata WHERE kind='movies' AND record_id='14'")
        self.store.set_kids_categories("movies", "14", self.modes(family="include", superheroes="include"))
        self.assertIn("14", self.ids(kids_category="family"))
        self.assertIn("14", self.ids(kids_category="superheroes"))
        connect = self.store.connect

        @contextmanager
        def indexed_connection():
            with connect() as db:
                def unexpected_classification(name, payload):
                    raise AssertionError("La clasificación debe reutilizar el índice.")
                db.create_function("KIDS_CATEGORIES", 2, unexpected_classification)
                yield db

        with patch.object(self.store, "connect", indexed_connection):
            self.assertEqual(self.ids(kids_category="anime"), {"11"})

    def test_deleted_records_do_not_leave_personal_overrides(self):
        self.store.set_kids_categories("movies", "14", self.modes(family="include"))
        changed = copy.deepcopy(self.data)
        changed["movies"] = [row for row in changed["movies"] if row["stream_id"] != 14]
        self.store.replace(changed)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM kids_category_overrides").fetchone()[0], 0)

    def test_move_from_detail_for_movies_and_series_and_undo(self):
        for kind, record_id in (("movies", "14"), ("series", "20")):
            self.cache(record_id, kind=kind, genres=["Drama"])
            url = f"/catalog/{kind}/{record_id}/kids-categories"
            detail = self.client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertIn("Pasar a contenido infantil", detail)
            self.assertIn('name="kids_target"', detail)
            self.assertIn('<summary class="small text-muted">Clasificación infantil</summary>', detail)
            for panel in ("kids-classification", "kids-category-settings"):
                self.assertNotIn("open", re.search(fr'<details id="{panel}"[^>]*>', detail).group())
            with self.client.session_transaction() as session:
                csrf = session["sync_csrf"]
            response = self.client.post(url, data={"csrf": csrf, "action": "move", "kids_target": "family"})
            self.assertEqual(response.status_code, 303)
            saved = self.client.get(response.location).get_data(as_text=True)
            self.assertIn("Título pasado a contenido infantil.", saved)
            for panel in ("kids-classification", "kids-category-settings"):
                self.assertNotIn("open", re.search(fr'<details id="{panel}"[^>]*>', saved).group())
            self.assertIn(record_id, self.ids(kind=kind, kids_category="family"))
            self.assertNotIn(record_id, {row["id"] for row in self.store.query(kind, children="exclude")["items"]})
            self.assertIn(record_id, {row["id"] for row in self.store.query(kind, children="all")["items"]})
            reopened = CatalogStore(self.path)
            self.assertNotIn(record_id, {row["id"] for row in reopened.query(kind, children="exclude")["items"]})
            self.store.set_kids_categories(kind, record_id, self.modes())
            self.assertIn(record_id, {row["id"] for row in self.store.query(kind, children="exclude")["items"]})

    def test_move_validation_shared_versions_and_existing_preferences(self):
        self.cache("14", id=10, title="Avengers", genres=["Acción"])
        self.store.set_kids_categories("movies", "14", self.modes(animation_hits="exclude"))
        self.store.set_watched("movies", "10", True)
        self.store.set_watchlisted("movies", "10", True)
        self.client.get("/catalog/movies/detail?id=14")
        with self.client.session_transaction() as session:
            csrf = session["sync_csrf"]
        url = "/catalog/movies/14/kids-categories"
        data = {"csrf": csrf, "action": "move", "kids_target": "superheroes"}
        self.assertEqual(self.client.post(url, data={**data, "csrf": ""}).status_code, 400)
        self.assertEqual(self.client.post(url, data={**data, "kids_target": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(url, data={**data, "action": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post("/catalog/movies/999/kids-categories", data=data).status_code, 404)
        self.assertEqual(self.client.post(url, data=data).status_code, 303)
        for record_id in ("10", "14"):
            settings = {item["id"]: item for item in self.store.kids_categories("movies", record_id)}
            self.assertEqual(settings["superheroes"]["mode"], "include")
            self.assertEqual(settings["animation_hits"]["mode"], "exclude")
            detail = self.store.detail("movies", record_id)
            self.assertTrue(detail["watched"])
            self.assertTrue(detail["watchlisted"])
            self.assertFalse(detail["banned"])
        self.assertNotIn("10", {row["id"] for row in self.store.query("movies", children="exclude")["items"]})
        self.store.replace(self.data)
        self.assertNotIn("10", {row["id"] for row in self.store.query("movies", children="exclude")["items"]})
        self.store.set_banned("movies", "10", True)
        self.assertEqual(self.client.post(url, data=data).status_code, 303)
        self.assertTrue(self.store.detail("movies", "10")["banned"])
        self.assertNotIn("10", self.ids(kids_category="superheroes"))

    def test_web_controls_api_pagination_and_preserved_detail_filters(self):
        for kind in ("movies", "series"):
            response = self.client.get(f"/kids/{kind}?kids_category=superheroes")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('id="kids-categories"', html)
            self.assertIn('form="filters" name="kids_category"', html)
            self.assertIn("Superhéroes", html)
            self.assertIn(f"/kids/{'series' if kind == 'movies' else 'movies'}?kids_category=superheroes", html)
            data = self.client.get(
                f"/api/catalog/{kind}?children=only&kids_category=superheroes&view=cards"
            ).json
            self.assertEqual(data["total"], 1)
            self.assertIn("kids_category=superheroes", data["html"])
        normal = self.client.get("/movies?children=all").get_data(as_text=True)
        self.assertNotIn('id="kids-categories"', normal)
        detail = self.client.get(
            "/catalog/movies/detail?id=10&children=only&kids_category=superheroes&page=2"
        ).get_data(as_text=True)
        self.assertIn("Guardar categorías", detail)
        self.assertIn("kids_category=superheroes", detail)
        self.assertIn("page=2", detail)

    def test_manual_route_validates_csrf_modes_kind_missing_record_and_preserves_navigation(self):
        self.client.get("/catalog/movies/detail?id=14")
        with self.client.session_transaction() as session:
            csrf = session["sync_csrf"]
        url = "/catalog/movies/14/kids-categories?children=only&kids_category=family&page=2"
        self.assertEqual(self.client.post(url, data=self.modes(family="include")).status_code, 400)
        data = {"csrf": csrf, **self.modes(family="include")}
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 303)
        self.assertIn("kids_category=family", response.location)
        self.assertIn("page=2", response.location)
        self.assertIn("14", self.ids(kids_category="family"))
        self.assertIn("Categorías guardadas.", self.client.get(response.location).get_data(as_text=True))
        invalid_page = self.client.post(
            "/catalog/movies/14/kids-categories?page=bad",
            data={"csrf": csrf, **self.modes(family="exclude")},
        )
        self.assertEqual(invalid_page.status_code, 400)
        self.assertIn("14", self.ids(kids_category="family"))
        self.assertEqual(self.client.post(url, data={**data, "family": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post("/catalog/movies/999/kids-categories", data=data).status_code, 404)
        self.assertEqual(self.client.post("/catalog/channels/14/kids-categories", data=data).status_code, 404)
        for url in (
            "/kids/movies?kids_category=invalid",
            "/api/catalog/movies?children=only&kids_category=invalid",
            "/movies?kids_category=family",
        ):
            self.assertEqual(self.client.get(url).status_code, 400, url)
        with self.assertRaises(ValueError):
            self.store.query("movies", kids_category="anime")
        with self.assertRaises(ValueError):
            self.store.set_kids_categories("movies", "10", {"anime": "include"})


if __name__ == "__main__":
    unittest.main()
