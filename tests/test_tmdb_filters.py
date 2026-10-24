import copy
import json
import re
import tempfile
import unittest
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from catalog import fingerprint, effective_people
from test_app import snapshot
from tmdb import TMDBError


class TMDBFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"][0].update(genre="Proveedor", cast="Actor proveedor", director="Director proveedor")
        self.store.replace(self.data)
        self.client = self.app.test_client()

    def cache(self, kind="movies", record_id="10", **changes):
        raw = self.store.detail(kind, record_id)["data"]
        payload = {
            "status": "matched", "id": int(record_id), "media": "movie" if kind == "movies" else "tv",
            "title": "Batman", "genres": ["Drama", "Crimen"], "release_date": "2024-02-03",
            "cast": [{"name": "Actor TMDB"}], "directors": ["Director TMDB"],
            "rating": 7.5, "votes": 200, **changes,
        }
        self.store.save_tmdb(kind, record_id, "CA", fingerprint(raw), payload)

    def test_filters_and_cards_use_tmdb_exclusively(self):
        self.cache()
        self.assertEqual(self.store.genres("movies"), ["Crimen", "Drama"])
        result = self.store.query("movies", genre=["Crimen"], actor="Actor TMDB", director="Director TMDB", era="recent")
        self.assertEqual(result["total"], 1)
        self.assertEqual((result["items"][0]["year"], result["items"][0]["genre"]), (2024, "Drama, Crimen"))
        for filters in ({"genre": "Proveedor"}, {"actor": "proveedor"}, {"director": "proveedor"}, {"era": "nostalgia"}):
            self.assertEqual(self.store.query("movies", **filters)["total"], 0)
        page = self.client.get("/movies?children=all").get_data(as_text=True)
        self.assertNotIn('name="category"', page)
        self.assertIn("Géneros de TMDB", page)
        self.assertNotIn("CAST. ANIMACION", page)

    def test_manual_tmdb_link_accepts_id_or_matching_url_and_persists(self):
        for kind, record_id, media in (("movies", "10", "movie"), ("series", "20", "tv")):
            self.cache(kind, record_id)
            original = self.store.detail(kind, record_id)["data"]
            page = self.client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertIn('id="tmdb-manual"', page)
            with self.client.session_transaction() as session:
                csrf = session["tmdb_csrf"]
            remote = Mock()
            remote.details.return_value = {
                "status": "matched", "id": 999, "media": media,
                "title": "Correct title", "genres": [], "countries": [], "companies": [],
            }
            self.app.config["TMDB_CLIENT_FACTORY"] = Mock(return_value=remote)
            url = f"/api/tmdb/{kind}/{record_id}"
            for reference in ("999", f"https://www.themoviedb.org/{media}/999-correct-title?language=es"):
                response = self.client.post(url, data={"csrf": csrf, "manual_tmdb": reference})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Correct title", response.json["html"])
                remote.details.assert_called_with(kind, 999)
            remote.search.assert_not_called()
            self.assertEqual(self.store.detail(kind, record_id)["data"], original)
            cached = self.client.get(url)
            self.assertIn("Correct title", cached.json["html"])
            self.assertEqual(remote.details.call_count, 2)

    def test_manual_tmdb_link_validation_csrf_and_failed_lookup_preserve_cache(self):
        self.cache()
        self.client.get("/catalog/movies/detail?id=10")
        with self.client.session_transaction() as session:
            csrf = session["tmdb_csrf"]
        url = "/api/tmdb/movies/10"
        remote = Mock()
        self.app.config["TMDB_CLIENT_FACTORY"] = Mock(return_value=remote)
        for reference in ("", "0", "-1", "1.2", "abc", "999999999999999999999",
                          "https://www.themoviedb.org/tv/999",
                          "https://example.com/movie/999", "https://www.themoviedb.org.evil/movie/999"):
            response = self.client.post(url, data={"csrf": csrf, "manual_tmdb": reference})
            self.assertEqual(response.status_code, 400, reference)
        self.assertEqual(self.client.post(url, data={"manual_tmdb": "999"}).status_code, 400)
        self.assertEqual(self.client.post(url, data={
            "csrf": csrf, "manual_tmdb": "999", "selected": "63",
        }).status_code, 400)
        remote.details.assert_not_called()
        remote.details.side_effect = TMDBError("No existe la ficha.")
        self.assertEqual(self.client.post(url, data={"csrf": csrf, "manual_tmdb": "999"}).status_code, 502)
        self.assertIn("Batman", self.client.get(url).json["html"])

    def test_kids_search_does_not_parse_people_for_non_kids_titles(self):
        data = snapshot()
        data["movies"] = [{"stream_id": number, "name": f"Title {number}"} for number in range(30)]
        self.store.replace(data)
        for number in range(30):
            self.cache(record_id=str(number), genres=["Familia" if number < 2 else "Drama"])
        original_connect = self.store.connect
        calls = []

        @contextmanager
        def counted_connect():
            with original_connect() as db:
                def people(raw, payload):
                    calls.append(payload)
                    return effective_people(raw, payload)
                db.create_function("PEOPLE_METADATA", 2, people)
                yield db

        with patch.object(self.store, "connect", counted_connect):
            result = self.store.query("movies", q="not-present", children="only", sort_by="rating", page=1)
        self.assertEqual(result["total"], 0)
        self.assertLessEqual(len(calls), 12)

    def test_rating_search_prepares_each_identity_and_people_only_once(self):
        for record_id in ("10", "11", "12"):
            self.cache(record_id=record_id)
        original_connect = self.store.connect
        identities, people = [], []

        @contextmanager
        def counted_connect():
            with original_connect() as db:
                def identity(raw):
                    identities.append(raw)
                    return fingerprint(json.loads(raw))

                def names(raw, payload):
                    people.append(payload)
                    return effective_people(raw, payload)

                db.create_function("TITLE_FINGERPRINT", 1, identity)
                db.create_function("PEOPLE_METADATA", 2, names)
                yield db

        with patch.object(self.store, "connect", counted_connect):
            result = self.store.query("movies", q="Actor TMDB", sort_by="rating", page=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(identities), 3)
        self.assertEqual(len(people), 3)
        self.assertIs(type(result["items"][0]["votes"]), int)

    def test_rating_order_weights_votes_without_changing_displayed_score(self):
        for kind, key in (("movies", "stream_id"), ("series", "series_id")):
            with self.subTest(kind=kind):
                data = snapshot()
                data[kind] = [
                    {key: number, "name": f"Title {number}"}
                    for number in range(1, 6)
                ]
                self.store.replace(data)
                for number, score, votes in ((1, 10, 1), (2, 8, 2000), (3, 4, 100),
                                              (4, 7, 100), (5, 10, 0)):
                    self.cache(kind, str(number), rating=score, votes=votes)
                descending = self.store.query(kind, sort_by="rating", sort="desc", page=1)
                self.assertEqual([item["id"] for item in descending["items"]], ["2", "1", "4", "3", "5"])
                self.assertEqual(descending["items"][1]["rating"], 10)
                self.assertEqual(descending["items"][1]["votes"], 1)
                ascending = self.store.query(kind, sort_by="rating", sort="asc")
                self.assertEqual([item["id"] for item in ascending["items"]], ["3", "4", "1", "2", "5"])
                page = self.client.get(f"/{kind}?children=all&watched=all").get_data(as_text=True)
                self.assertIn("Más fiables primero", page)
                self.assertEqual(page.count("Pocos votos"), 1)
                html = self.client.get(f"/api/tmdb/{kind}/1").json["html"]
                self.assertIn("Pocos votos", html)
                html = self.client.get(f"/api/tmdb/{kind}/4").json["html"]
                self.assertNotIn("Pocos votos", html)

    def test_rating_order_handles_no_valid_scores(self):
        result = self.store.query("movies", sort_by="rating", sort="desc", page=1)
        self.assertEqual(result["total"], 4)
        self.assertTrue(all(item["rating"] is None and item["votes"] is None for item in result["items"]))

    def test_unified_search_matches_title_actor_or_director(self):
        self.data["movies"] = [
            {"stream_id": 10, "name": "Corazón valiente"},
            {"stream_id": 11, "name": "La pasión de Cristo"},
            {"stream_id": 12, "name": "Gibson documental"},
            {"stream_id": 13, "name": "Otra película"},
        ]
        self.store.replace(self.data)
        self.cache(record_id="10", cast=[{"name": "Mel Gibson"}], directors=[])
        self.cache(record_id="11", cast=[], directors=["Mel Gibson"])
        result = self.store.query("movies", q="gIbSoN")
        self.assertEqual({item["id"] for item in result["items"]}, {"10", "11", "12"})
        self.assertEqual(self.store.query("movies", q="Gibson", genre="Drama")["total"], 2)
        self.assertEqual(self.store.query("movies", q="Gibson", director="Mel")["total"], 1)
        self.assertEqual(self.store.query("movies", q="' OR 1=1 --")["total"], 0)
        self.store.set_movie_banned("10", True)
        self.assertEqual(self.store.query("movies", q="Gibson")["total"], 2)
        self.cache(record_id="13", cast=[{"name": "Gibson"}], status="candidates")
        self.assertEqual(self.store.query("movies", q="Gibson")["total"], 2)
        page = self.client.get("/movies?q=gibson").get_data(as_text=True)
        self.assertLess(page.index('id="q"'), page.index('id="catalog-filters"'))
        self.assertIn('form="filters"', page)
        self.assertNotIn('id="actor"', page)
        self.assertNotIn('id="director"', page)

    def test_search_uses_cached_original_title_and_creators_and_literal_characters(self):
        self.cache(title="Título traducido", original_title="Original unique", creators=["Gibson"], cast=[], directors=[])
        self.assertEqual(self.store.query("movies", q="Original unique")["total"], 1)
        self.assertEqual(self.store.query("movies", q="gibson")["total"], 1)
        self.assertEqual(self.store.query("movies", q="%_")["total"], 1)
        self.assertEqual(self.store.query("movies", q="Actor proveedor")["total"], 0)

    def test_availability_only_shows_nonempty_access_groups(self):
        self.cache(providers={"flatrate": ["Netflix"], "rent": [], "buy": []})
        html = self.client.get("/api/tmdb/movies/10").json["html"]
        self.assertIn('data-access="flatrate"', html)
        self.assertIn('alt="Netflix"', html)
        for access in ("rent", "buy", "free", "ads"):
            self.assertNotIn(f'data-access="{access}"', html)
        self.assertNotIn("Sin disponibilidad confirmada", html)
        self.cache(providers={})
        html = self.client.get("/api/tmdb/movies/10").json["html"]
        self.assertEqual(html.count("Sin disponibilidad confirmada"), 1)
        self.assertIn("Ver disponibilidad en TMDB / JustWatch", html)

    def test_rating_is_prominent_accessible_and_handles_missing_or_zero_scores(self):
        for rating, votes, expected in ((8, 200, "80%"), (0, 10, "0%"), (7.5, 0, None),
                                        (None, 10, None), (11, 10, None)):
            with self.subTest(rating=rating, votes=votes):
                self.cache(rating=rating, votes=votes)
                html = self.client.get("/api/tmdb/movies/10").json["html"]
                self.assertLess(html.index("tmdb-score"), html.index("<table"))
                self.assertNotIn("<th scope=\"row\">Valoración TMDB</th>", html)
                if expected:
                    self.assertIn(f'class="score-value">{expected}</span>', html)
                    self.assertIn(f'{votes} votos en TMDB', html)
                else:
                    self.assertIn("Sin valoración disponible en TMDB", html)

    def test_unmatched_detail_shows_collapsible_provider_metadata(self):
        for status in ("candidates", "missing"):
            self.cache(status=status, candidates=[])
            detail = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
            block = re.search(r'<details id="provider-metadata"([^>]*)>(.*?)</details>', detail, re.S)
            self.assertIsNotNone(block, status)
            self.assertNotIn("hidden", block.group(1))
            self.assertNotIn(" open", block.group(1))
            self.assertIn("Metadatos del proveedor", block.group(2))
            self.assertIn('data-field="genre"', block.group(2))
            self.assertIn("Director proveedor", block.group(2))
            self.assertEqual(self.client.get("/api/tmdb/movies/10").json["status"], status)

    def test_missing_unconfirmed_and_stale_metadata_never_fall_back(self):
        self.assertEqual(self.store.query("movies")["total"], 4)
        self.assertEqual(self.store.query("movies", era="nostalgia")["total"], 0)
        for status in ("missing", "candidates"):
            self.cache(status=status)
            self.assertEqual(self.store.genres("movies"), [])
            self.assertEqual(self.store.query("movies", genre="Drama")["total"], 0)
        self.cache()
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "New identity"
        self.store.replace(changed)
        self.assertEqual(self.store.query("movies", genre="Drama")["total"], 0)
        self.assertEqual(self.store.genres("movies"), [])

    def test_year_sort_and_children_follow_tmdb(self):
        self.cache(release_date="2024-01-01", genres=["Familia"])
        self.cache(record_id="11", release_date="1995-01-01", genres=["Drama"])
        self.assertEqual(self.store.query("movies", sort_by="year", sort="asc")["items"][0]["id"], "11")
        self.assertEqual(self.store.query("movies", children="only")["items"][0]["id"], "10")
        self.assertNotIn("10", [item["id"] for item in self.store.query("movies", children="exclude")["items"]])

    def test_genres_filter_before_count_and_pagination_with_or_selection(self):
        data = snapshot()
        data["movies"] = [
            {"stream_id": number, "name": f"Movie {number:03}", "genre": "Drama"}
            for number in range(60)
        ]
        self.store.replace(data)
        for number in range(55):
            self.cache(record_id=str(number), genres=["Drama" if number < 50 else "Comedia"])
        result = self.store.query("movies", genre=["drama", "Comedia"], page=2)
        self.assertEqual((result["total"], result["pages"], len(result["items"])), (55, 2, 5))
        self.assertEqual(self.store.query("movies", genre="Drama")["total"], 50)
        payload = self.client.get("/api/catalog/movies?view=cards&genre=Drama&genre=Comedia&page=2").json
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (55, 2, 5))

    def test_detail_only_has_tmdb_and_genres_link_to_working_filters(self):
        for kind, record_id in (("movies", "10"), ("series", "20")):
            self.cache(kind, record_id)
            detail = self.client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertRegex(detail, r'<details id="provider-metadata"[^>]*\s+hidden>')
            self.assertIn('id="tmdb-section"', detail)
            html = self.client.get(f"/api/tmdb/{kind}/{record_id}").json["html"]
            links = re.findall(r'class="genre-link" href="([^"]+)"', html)
            self.assertEqual(len(links), 2)
            for link in links:
                page = self.client.get(unescape(link))
                self.assertEqual(page.status_code, 200)
                self.assertIn(b"Batman", page.data)
