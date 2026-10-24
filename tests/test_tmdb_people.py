import copy
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app import create_app
from catalog import fingerprint
from test_app import snapshot


class TMDBPeopleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"][0].update(cast="IPTV Actor", director="IPTV Director")
        self.store.replace(self.data)
        self.client = self.app.test_client()

    def cache(self, kind="movies", record_id="10", country="CA", status="matched", **fields):
        raw = self.store.detail(kind, record_id)["data"]
        self.store.save_tmdb(kind, record_id, country, fingerprint(raw), {
            "status": status, "id": int(record_id), "media": "movie" if kind == "movies" else "tv",
            **fields,
        })

    def test_tmdb_people_are_exclusive_without_provider_fallback(self):
        self.cache(cast=[{"name": "Élodie Willis", "character": "The Hero"}], directors=["Terry Gilliam"])
        self.assertEqual(self.store.query("movies", actor="élOD", director="gILLI")["total"], 1)
        self.assertEqual(self.store.query("movies", actor="IPTV")["total"], 0)
        self.assertEqual(self.store.query("movies", director="IPTV")["total"], 0)
        self.assertEqual(self.store.query("movies", actor="The Hero")["total"], 0)
        self.assertEqual(self.store.query("movies", person="ÉLODIE WILLIS")["total"], 1)
        self.assertEqual(self.store.query("movies", person="Élodie")["total"], 0)
        self.cache(cast=[], directors=["Terry Gilliam"])
        self.assertEqual(self.store.query("movies", actor="IPTV", director="terry")["total"], 0)
        self.cache(cast=[{"name": "New Actor"}], directors=[])
        self.assertEqual(self.store.query("movies", actor="New", director="IPTV")["total"], 0)
        self.assertEqual(self.store.detail("movies", "10")["data"], self.data["movies"][0])

    def test_unconfirmed_other_country_and_stale_data_are_not_filterable(self):
        for status in ("candidates", "missing"):
            self.cache(status=status, cast=[{"name": "Wrong Actor"}])
            self.assertEqual(self.store.query("movies", actor="Wrong")["total"], 0)
            self.assertEqual(self.store.query("movies", actor="IPTV")["total"], 0)
        self.cache(country="US", cast=[{"name": "Wrong Actor"}])
        self.assertEqual(self.store.query("movies", actor="Wrong")["total"], 0)
        self.cache(cast=[{"name": "Wrong Actor"}])
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "Different identity"
        self.store.replace(changed)
        self.assertEqual(self.store.query("movies", actor="Wrong")["total"], 0)
        self.assertEqual(self.store.query("movies", actor="IPTV")["total"], 0)

    def test_clickable_cast_directors_creators_and_exact_person_results(self):
        actor = 'Actor & <Name>'
        self.cache(cast=[{"name": actor, "character": "Hero"}], directors=["Director Name"])
        self.cache("series", "20", cast=[{"name": actor}], creators=["Creator Name"])
        payload = self.client.get("/api/tmdb/movies/10").json
        self.assertIn("Actor &amp; &lt;Name&gt;", payload["html"])
        self.assertIn('data-replaces-field="cast"', payload["html"])
        self.assertIn('data-replaces-field="director"', payload["html"])
        links = re.findall(r'<a class="person-link" href="([^"]+)">(.*?)</a>', payload["html"])
        self.assertEqual(len(links), 2)
        for url, label in links:
            name = parse_qs(urlsplit(unescape(url)).query)["name"][0]
            self.assertEqual(name, unescape(label))
            html = self.client.get(unescape(url)).get_data(as_text=True)
            self.assertIn("Batman", html)
        html = self.client.get("/people", query_string={"name": actor}).get_data(as_text=True)
        self.assertIn("Películas (1)", html)
        self.assertIn("Series (1)", html)
        tv = self.client.get("/api/tmdb/series/20").json["html"]
        self.assertIn("Creator Name</a>", tv)
        self.assertEqual(self.store.query("series", person="Creator Name")["total"], 1)
        self.assertEqual(self.store.query("series", director="Creator Name")["total"], 0)

    def test_literal_search_combined_filters_and_bans(self):
        self.cache(cast=[{"name": "100%_Actor"}], directors=["A Director"])
        self.assertEqual(self.store.query("movies", actor="%_", director="Director", category="1")["total"], 1)
        self.assertEqual(self.store.query("movies", actor="' OR 1=1 --")["total"], 0)
        payload = self.client.get("/api/catalog/movies?view=cards&actor=%25_&director=Director").json
        self.assertEqual(payload["total"], 1)
        self.store.set_movie_banned("10", True)
        self.assertEqual(self.store.query("movies", person="100%_Actor")["total"], 0)

    def test_tmdb_people_filter_before_count_and_pagination(self):
        self.data["movies"] = [{"stream_id": i, "name": f"Movie {i:03}"} for i in range(60)]
        self.store.replace(self.data)
        for i in range(55):
            self.cache(record_id=str(i), cast=[{"name": "Common Actor"}], directors=["Common Director"])
        payload = self.client.get("/api/catalog/movies?view=cards&actor=common&director=common&page=2").json
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (55, 2, 5))
        self.assertIn("actor=common", payload["html"])
        result = self.store.query("movies", person="Common Actor", page=2)
        self.assertEqual((result["total"], len(result["items"])), (55, 5))
