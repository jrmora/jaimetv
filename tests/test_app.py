import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import requests

from app import create_app, main
from catalog import CatalogStore, fingerprint
from xtream import ACTIONS, CatalogError, XtreamClient


def detail_fields(html):
    from html import unescape
    import re

    fields = {
        unescape(key): json.loads(unescape(value))
        for key, value in re.findall(
            r'<th scope="row" data-field="([^"]+)">[^<]*</th><td>\s*<pre[^>]*>(.*?)</pre>\s*</td>', html, re.S
        )
    }
    for key, cell in re.findall(r'<th scope="row" data-field="([^"]+)">[^<]*</th><td>(.*?)</td>', html, re.S):
        names = [unescape(name) for name in re.findall(r'<a class="category-link"[^>]*>(.*?)</a>', cell)]
        if names:
            fields[key] = names[0] if key == "category_name" else names
    return fields


def snapshot():
    data = {table: [] for table in ACTIONS}
    for table in ("live_categories", "vod_categories", "series_categories"):
        data[table] = [
            {"category_id": "1", "category_name": "Acción", "parent_id": 0, "extra": {"keep": True}},
            {"category_id": "2", "category_name": "Drama"},
        ]
    data["live_streams"] = [
        {"stream_id": 1, "name": "Batman TV", "category_id": "1", "stream_icon": "https://example.invalid/icon.png"},
        {"stream_id": 2, "name": "Noticias", "category_id": "404"},
    ]
    data["movies"] = [
        {
            "stream_id": 10, "name": "Batman", "category_id": "1",
            "rating": "8.5", "year": "1989", "container_extension": "mp4",
            "unknown": {"nested": "preservado"}, "tags": ["hero", "acción"],
        },
        {"stream_id": 11, "name": "ÁRBOL", "category_id": "2", "rating": "N/A"},
        {"stream_id": 12, "name": "100%_literal", "category_id": None},
        {"stream_id": 13, "name": "<script>alert(1)</script>", "category_id": "2"},
    ]
    data["series"] = [
        {
            "series_id": 20, "name": "Batman: la serie", "category_id": "1",
            "cover": "https://example.invalid/cover.png", "rating": "9",
            "releaseDate": "1992-09-05", "genre": "Animación",
            "seasons": [{"number": 1}],
        },
    ]
    return data


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.store = CatalogStore(self.path)
        self.data = snapshot()
        self.store.replace(self.data)

    def test_json_and_normalized_fields_preserved_for_all_tables(self):
        with self.store.connect() as db:
            for table, rows in self.data.items():
                actual = [json.loads(row[0]) for row in db.execute(f"SELECT raw_json FROM {table}")]
                self.assertEqual(actual, rows)
            movie = db.execute("SELECT * FROM movies WHERE stream_id = '10'").fetchone()
            self.assertEqual(movie["rating"], 8.5)
            self.assertEqual(movie["year"], "1989")
            series = db.execute("SELECT * FROM series").fetchone()
            self.assertEqual(series["release_date"], "1992-09-05")
            self.assertEqual(series["genre"], "Animación")
            self.assertTrue(self.store.stats()["synced_at"].endswith("+00:00"))

    def test_search_all_kinds_unicode_and_literal_wildcards(self):
        for kind in ("channels", "movies", "series"):
            self.assertEqual(self.store.query(kind, q="BATMAN")["total"], 1)
        self.assertEqual(self.store.query("movies", q="árbol")["total"], 1)
        self.assertEqual(self.store.query("movies", q="%_")["total"], 1)
        self.assertEqual(self.store.query("movies", q="' OR 1=1 --")["total"], 0)

    def test_filter_sort_without_pagination(self):
        self.assertEqual(self.store.query("movies", category="1")["items"][0]["name"], "Batman")
        self.assertEqual(self.store.query("movies", category="__uncategorized__")["total"], 1)
        result = self.store.query("movies")
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["items"]), 4)
        asc = self.store.query("movies")["items"]
        desc = self.store.query("movies", sort="desc")["items"]
        self.assertEqual([item["id"] for item in desc], [item["id"] for item in asc][::-1])

    def test_column_sorting_is_global_numeric_and_nulls_last(self):
        for kind, (table, id_key) in {
            "movies": ("movies", "stream_id"),
            "series": ("series", "series_id"),
            "channels": ("live_streams", "stream_id"),
        }.items():
            data = snapshot()
            data[table] = [
                {id_key: 1, "name": "Zulu", "category_id": "1", "year": "9", "rating": "9"},
                {id_key: 2, "name": "Alpha", "category_id": "2", "year": "100", "rating": "10"},
                {id_key: 3, "name": "Missing", "category_id": "404"},
            ]
            self.store.replace(data)
            if kind in {"movies", "series"}:
                for row in data[table][:2]:
                    self.store.save_tmdb(kind, str(row[id_key]), "CA", fingerprint(row), {
                        "status": "matched", "rating": float(row["rating"]), "votes": 100,
                    })
            for column in ("name", "category_name", "year", "rating"):
                for direction in ("asc", "desc"):
                    with self.subTest(kind=kind, column=column, direction=direction):
                        result = self.store.query(kind, sort_by=column, sort=direction)
                        if kind == "channels" and column == "rating":
                            self.assertTrue(all(item["rating"] is None for item in result["items"]))
                            continue
                        expected = "2" if column == "name" else "1"
                        if direction == "desc":
                            expected = "1" if column == "name" else "3" if column == "category_name" else "2"
                        self.assertEqual(result["items"][0]["id"], expected)
                        self.assertEqual(result["total"], 3)
                        if column in {"year", "rating"}:
                            last = self.store.query(kind, sort_by=column, sort=direction)
                            self.assertEqual(last["items"][-1]["id"], "3")
        with self.assertRaises(ValueError):
            self.store.query("movies", sort_by="name; DROP TABLE movies")

    def test_column_sorting_routes_and_no_javascript_links(self):
        from html import unescape
        from urllib.parse import urlsplit, parse_qs
        import re

        client = create_app(self.path).test_client()
        for kind in ("movies", "series", "channels"):
            response = client.get(f"/{kind}?sort_by=rating&sort=desc&q=batman&category=1&per_page=10")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('value="rating" selected', html)
            self.assertIn('value="desc" selected', html)
            links = re.findall(r'href="([^"]+)"', html)
            toggle = next(unescape(link) for link in links if f"/catalog/{kind}/detail?" in link)
            params = parse_qs(urlsplit(toggle).query)
            self.assertEqual(params["q"], ["batman"])
            self.assertEqual(params["category"], ["1"])
            self.assertEqual(params["page"], ["1"])
            self.assertEqual(client.get(toggle).status_code, 200)
            self.assertEqual(client.get(f"/api/catalog/{kind}?sort_by=rating&sort=desc").status_code, 200)
            for prefix in (f"/{kind}", f"/api/catalog/{kind}"):
                self.assertEqual(client.get(prefix + "?sort_by=invalid").status_code, 400)
        for column in ("kind", "category", "total"):
            for direction in ("asc", "desc"):
                response = client.get(f"/dashboard?sort_by={column}&sort={direction}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/movies")
        self.assertEqual(client.get("/dashboard?sort_by=invalid").status_code, 302)

    def test_details_filter_display_fields_without_changing_stored_data(self):
        from html import unescape
        import re

        data = snapshot()
        data["movies"][0]["direct_source"] = "https://example.invalid/video?user=demo&token=example"
        data["movies"][0]["plot"] = '<img src=x onerror="alert(1)">'
        data["movies"][0]["nullable"] = None
        data["movies"][0]["enabled"] = False
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind, table, record_id in (("movies", "movies", "10"), ("series", "series", "20"), ("channels", "live_streams", "1")):
            with self.subTest(kind=kind):
                original = data[table][0]
                self.assertEqual(self.store.detail(kind, record_id)["data"], original)
                response = client.get(f"/catalog/{kind}/detail", query_string={
                    "id": record_id, "q": "batman", "category": "1", "page": "2",
                    "per_page": "10", "sort_by": "rating", "sort": "desc", "view": "table",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                html = response.get_data(as_text=True)
                expected = {key: value for key, value in original.items()
                            if key not in {"stream_icon", "cover", "backdrop_path", "nullable", "num", "stream_id", "series_id", "container_extension", "cast", "director"}}
                year = expected.pop("year", None)
                release = expected.pop("releaseDate", None)
                if year:
                    expected["premiere"] = year
                if release:
                    expected["premiere"] = (expected["premiere"] + " · " if "premiere" in expected else "") + "05/09/1992"
                expected.pop("rating", None)
                expected.pop("name", None)
                expected.pop("category_id")
                expected["category_name"] = "Acción"
                self.assertEqual(detail_fields(html), expected)
                self.assertIn('id="theme-toggle"', html)
                self.assertNotIn('<img src=x', html)
                back = re.search(r'id="back-button"[^>]*href="([^"]+)"', html).group(1)
                query = parse_qs(urlsplit(unescape(back)).query)
                self.assertEqual(query["page"], ["2"])
                self.assertEqual(query["view"], ["cards"])
                self.assertEqual(query["q"], ["batman"])
                self.assertEqual(query["sort_by"], ["rating"])
                listing = client.get(f"/{kind}?q=batman").get_data(as_text=True)
                self.assertIn(f'/catalog/{kind}/detail?id={record_id}', listing)
        for path in ("/catalog/movies/detail?id=missing", "/catalog/movies/detail", "/catalog/invalid/detail?id=10", "/catalog/movies/detail?id=' OR 1=1 --"):
            self.assertEqual(client.get(path).status_code, 404)

    def test_detail_poster_uses_icon_or_cover_and_rejects_invalid_urls(self):
        client = create_app(self.path).test_client()
        for value in ("https://example.invalid/poster.jpg?x=1&y=2", "http://example.invalid/poster.png", "javascript:alert(1)", "//example.invalid/a", "https://[bad", "", None, ["https://example.invalid/a"]):
            with self.subTest(value=value):
                data = snapshot()
                data["movies"][0]["stream_icon"] = value
                self.store.replace(data)
                response = client.get("/catalog/movies/detail?id=10")
                self.assertEqual(response.status_code, 200)
                valid = isinstance(value, str) and value.startswith(("https://example.invalid/", "http://example.invalid/"))
                self.assertEqual(b'id="detail-poster"' in response.data, valid)
                self.assertIn("http: https:", response.headers["Content-Security-Policy"])
                if valid:
                    self.assertIn(b'referrerpolicy="no-referrer"', response.data)
                    self.assertIn(b'id="poster-error"', response.data)
        response = client.get("/catalog/series/detail?id=20")
        self.assertIn(b'src="https://example.invalid/cover.png"', response.data)
        self.assertNotIn("http: https:", client.get("/dashboard").headers["Content-Security-Policy"])

    def test_series_show_cover_and_unique_valid_backdrops(self):
        client = create_app(self.path).test_client()
        for value, expected in (
            (["https://example.invalid/background.jpg", "https://example.invalid/background.jpg", "javascript:alert(1)", None, "https://example.invalid/cover.png"], 1),
            ("https://example.invalid/background.jpg", 1),
            (["https://example.invalid/one.jpg", "http://example.invalid/two.jpg"], 2),
            (None, 0), ({"invalid": "value"}, 0), ([], 0),
        ):
            with self.subTest(value=value):
                data = snapshot()
                data["series"][0]["backdrop_path"] = value
                self.store.replace(data)
                response = client.get("/catalog/series/detail?id=20")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'src="https://example.invalid/cover.png"', response.data)
                self.assertEqual(response.data.count(b'data-error-id="backdrop-error-'), expected)
                self.assertNotIn(b'src="javascript:', response.data)
        data = snapshot()
        data["series"][0].pop("cover")
        data["series"][0]["backdrop_path"] = ["https://example.invalid/background.jpg"]
        self.store.replace(data)
        response = client.get("/catalog/series/detail?id=20")
        self.assertIn(b'src="https://example.invalid/background.jpg"', response.data)

    def test_detail_hides_empty_fields_and_image_urls_but_keeps_images(self):
        from html import unescape
        import re

        data = snapshot()
        data["series"][0].update({
            "plot": "", "cast": " \t\n", "director": None,
            "empty_list": [], "empty_object": {}, "rating": 0, "enabled": False,
            "episode_run_time": "0", "backdrop_path": ["https://example.invalid/background.jpg"],
        })
        self.store.replace(data)
        html = create_app(self.path).test_client().get("/catalog/series/detail?id=20").get_data(as_text=True)
        visible = detail_fields(html)
        for field in ("plot", "cast", "director", "empty_list", "empty_object", "cover", "backdrop_path"):
            self.assertNotIn(field, visible)
            self.assertNotIn(f'data-field="{field}"', html)
        self.assertNotIn("rating", visible)
        self.assertIs(visible["enabled"], False)
        self.assertEqual(visible["episode_run_time"], "0")
        self.assertIn('src="https://example.invalid/cover.png"', html)
        self.assertIn('src="https://example.invalid/background.jpg"', html)
        self.assertEqual(self.store.detail("series", "20")["data"], data["series"][0])

    def test_detail_resolves_primary_and_multiple_categories_by_content_type(self):
        from html import unescape
        import re

        data = snapshot()
        for kind, table, category_table, record_id in (
            ("movies", "movies", "vod_categories", "10"),
            ("series", "series", "series_categories", "20"),
            ("channels", "live_streams", "live_categories", "1"),
        ):
            data[category_table][0]["category_name"] = f"Categoría {kind}"
            data[table][0]["category_ids"] = [2, "1", 999]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind, table, record_id in (("movies", "movies", "10"), ("series", "series", "20"), ("channels", "live_streams", "1")):
            with self.subTest(kind=kind):
                html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
                visible = detail_fields(html)
                self.assertEqual(visible["category_name"], f"Categoría {kind}")
                self.assertEqual(visible["category_names"], ["Drama", f"Categoría {kind}", "Categoría desconocida (ID 999)"])
                self.assertNotIn("category_id", visible)
                self.assertNotIn("category_ids", visible)
                self.assertEqual(self.store.detail(kind, record_id)["data"], data[table][0])
        html = client.get("/catalog/channels/detail?id=2").get_data(as_text=True)
        self.assertIn("Categoría desconocida (ID 404)", html)

    def test_detail_hides_redundant_stream_type_for_movies_and_series(self):
        from html import unescape
        import re

        data = snapshot()
        data["movies"][0]["stream_type"] = "movie"
        data["series"][0]["stream_type"] = "series"
        data["live_streams"][0]["stream_type"] = "live"
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind, table, record_id in (("movies", "movies", "10"), ("series", "series", "20"), ("channels", "live_streams", "1")):
            with self.subTest(kind=kind):
                html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
                self.assertEqual("stream_type" in detail_fields(html), kind == "channels")
                self.assertEqual('data-field="stream_type"' in html, kind == "channels")
                self.assertEqual(self.store.detail(kind, record_id)["data"], data[table][0])

    def test_detail_table_hides_ids_and_has_no_json_block(self):
        from html import unescape
        import re

        data = snapshot()
        data["movies"][0]["num"] = 1
        self.store.replace(data)
        html = create_app(self.path).test_client().get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertNotIn('data-field="num"', html)
        self.assertNotIn('data-field="stream_id"', html)
        visible = detail_fields(html)
        self.assertNotIn("num", visible)
        self.assertNotIn("stream_id", visible)
        self.assertNotIn("container_extension", visible)
        self.assertNotIn('id="record-json"', html)
        series_html = create_app(self.path).test_client().get("/catalog/series/detail?id=20").get_data(as_text=True)
        self.assertNotIn("series_id", detail_fields(series_html))
        self.assertNotIn('id="record-json"', series_html)
        self.assertEqual(self.store.detail("series", "20")["data"], data["series"][0])
        self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])

    def test_detail_formats_unix_dates_in_utc_without_changing_database(self):
        client = create_app(self.path).test_client()
        for kind, table, record_id in (("movies", "movies", "10"), ("series", "series", "20"), ("channels", "live_streams", "1")):
            for value, expected in (
                ("1720531867", "09/07/2024"),
                (1720531867, "09/07/2024"),
                (0, "01/01/1970"),
                ("invalid", "Fecha no válida: invalid"),
                ("1e100", "Fecha no válida: 1e100"),
                ("NaN", "Fecha no válida: NaN"),
                (True, "Fecha no válida: True"),
            ):
                with self.subTest(kind=kind, value=value):
                    data = snapshot()
                    for key in ("added", "modified", "last_modified"):
                        data[table][0][key] = value
                    self.store.replace(data)
                    html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
                    fields = detail_fields(html)
                    self.assertNotIn("added", fields)
                    for key in ("modified", "last_modified"):
                        self.assertEqual(fields[key], expected)
                    self.assertEqual(self.store.detail(kind, record_id)["data"], data[table][0])
        data = snapshot()
        data["movies"][0].update({"added": "", "modified": None, "last_modified": " "})
        self.store.replace(data)
        fields = detail_fields(client.get("/catalog/movies/detail?id=10").get_data(as_text=True))
        for key in ("added", "modified", "last_modified"):
            self.assertNotIn(key, fields)

    def test_actor_director_filters_partial_casefold_literal_and_combined(self):
        data = snapshot()
        for table in ("movies", "series"):
            data[table][0].update({"cast": "Bruce Willis, BRAD Pitt, Élodie", "director": "Terry Gilliam, Lê Hoàng"})
        data["movies"][1].update({"cast": "100%_Actor", "director": None})
        self.store.replace(data)
        for kind in ("movies", "series"):
            for filters in ({"actor": "wILL"}, {"actor": "élOD"}, {"director": "GILLI"}, {"director": "lÊ"}, {"actor": "pitt", "director": "terry", "q": "BATMAN", "category": "1"}):
                with self.subTest(kind=kind, filters=filters):
                    self.assertEqual(self.store.query(kind, **filters)["total"], 1)
            self.assertEqual(self.store.query(kind, actor="willis", director="unknown")["total"], 0)
        self.assertEqual(self.store.query("movies", actor="%_")["total"], 1)
        self.assertEqual(self.store.query("movies", actor="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.store.query("movies", actor="willis", category="2")["total"], 0)

    def test_people_filters_web_api_and_links_preserve_context(self):
        from html import unescape
        import re

        data = snapshot()
        data["movies"][0].update({"cast": "Bruce Willis", "director": "Terry Gilliam"})
        data["movies"].append({"stream_id": 90, "name": "Batman Returns", "category_id": "1", "cast": "Bruce Willis", "director": "Terry Gilliam"})
        self.store.replace(data)
        client = create_app(self.path).test_client()
        query = {"actor": "WILL", "director": "gilli", "per_page": 1, "q": "batman", "category": "1"}
        response = client.get("/api/catalog/movies", query_string=query)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total"], 2)
        self.assertEqual(len(response.get_json()["items"]), 2)
        html = client.get("/movies", query_string=query).get_data(as_text=True)
        self.assertIn('name="actor"', html)
        self.assertIn('name="director"', html)
        for href in re.findall(r'href="([^"]+)"', html):
            url = unescape(href)
            if "page=2" in url or "/detail?" in url or "sort_by=rating" in url:
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params["actor"], ["WILL"])
                self.assertEqual(params["director"], ["gilli"])
                self.assertEqual(client.get(url).status_code, 200)
                if "/detail?" in url:
                    detail = client.get(url).get_data(as_text=True)
                    back = unescape(re.search(r'id="back-button"[^>]*href="([^"]+)"', detail).group(1))
                    self.assertEqual(parse_qs(urlsplit(back).query)["actor"], ["WILL"])
        for field in ("actor", "director"):
            self.assertEqual(client.get("/api/catalog/movies", query_string={field: "x" * 201}).status_code, 400)
        self.assertNotIn('name="actor"', client.get("/channels").get_data(as_text=True))

    def test_person_links_deduplicate_names_and_find_both_roles_and_types(self):
        from html import unescape
        import re
        from catalog import people_names

        self.assertEqual(people_names(" Christopher Nolan, christopher nolan, Christopher Nolan, "), ["Christopher Nolan"])
        self.assertEqual(people_names(["Matt Damon", "matt damon", None, "Tom Holland"]), ["Matt Damon", "Tom Holland"])
        data = snapshot()
        data["movies"][0]["director"] = "Christopher Nolan, christopher nolan, Christopher Nolan"
        data["movies"][0]["cast"] = "Matt Damon, Matt Damon, Tom Holland"
        data["movies"][1]["cast"] = "Christopher Nolan"
        data["movies"][2]["director"] = "Christopher Nolan Jr."
        data["series"][0]["cast"] = ["Christopher Nolan", "Christopher Nolan"]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        links = re.findall(r'<a class="person-link" href="([^"]+)">(.*?)</a>', html)
        self.assertEqual([unescape(name) for _, name in links].count("Christopher Nolan"), 1)
        self.assertEqual([unescape(name) for _, name in links].count("Matt Damon"), 1)
        link = next(unescape(url) for url, name in links if name == "Christopher Nolan")
        response = client.get(link)
        self.assertEqual(response.status_code, 200)
        result = response.get_data(as_text=True)
        self.assertIn("Películas (2)", result)
        self.assertIn("Series (1)", result)
        self.assertIn("Batman: la serie", result)
        self.assertEqual(self.store.query("movies", person="CHRISTOPHER NOLAN")["total"], 2)
        self.assertEqual(self.store.query("series", person="christopher nolan")["total"], 1)
        self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])
        self.assertEqual(client.get("/people?name=Nobody").status_code, 200)
        for query in ({}, {"name": " "}, {"name": "a" * 201}):
            self.assertEqual(client.get("/people", query_string=query).status_code, 400)
        data["movies"] = [{"stream_id": 100 + i, "name": f"Movie {i:02}", "cast": "Christopher Nolan"} for i in range(27)]
        self.store.replace(data)
        all_results = client.get(link + "&view=table").get_data(as_text=True)
        self.assertIn("Página 1 de 1", all_results)
        self.assertIn("Movie 26", all_results)
        self.assertIn("Movie 00", all_results)
        self.assertIn("Batman: la serie", all_results)

    def test_category_links_find_primary_and_secondary_memberships(self):
        from html import unescape
        import re

        data = snapshot()
        for table in ("movies", "series", "live_streams"):
            data[table][0]["category_ids"] = [2, "2", "1", 999]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind, table, record_id in (("movies", "movies", "10"), ("series", "series", "20"), ("channels", "live_streams", "1")):
            with self.subTest(kind=kind):
                html = client.get(f"/catalog/{kind}/detail?id={record_id}&q=irrelevant&actor=other").get_data(as_text=True)
                links = [(unescape(url), unescape(name)) for url, name in re.findall(r'<a class="category-link" href="([^"]+)">(.*?)</a>', html)]
                secondary = next(url for url, name in links if name == "Drama")
                self.assertEqual(sum(name == "Drama" for _, name in links), 1)
                self.assertEqual(parse_qs(urlsplit(secondary).query), {"category": ["2"], "foreign": ["0"]})
                response = client.get(secondary)
                self.assertEqual(response.status_code, 200)
                self.assertIn(data[table][0]["name"], response.get_data(as_text=True))
                matches = self.store.query(kind, category="2")
                expected = 3 if kind == "movies" else 1
                self.assertEqual(matches["total"], expected)
                self.assertEqual(len(matches["items"]), expected)
                unknown = next(url for url, name in links if "999" in name)
                self.assertEqual(client.get(unknown).status_code, 200)
                self.assertEqual(self.store.query(kind, category="999")["total"], 1)
                self.assertEqual(self.store.detail(kind, record_id)["data"], data[table][0])

    def test_shared_back_arrow_and_theme_icon_on_all_pages(self):
        import re

        client = create_app(self.path).test_client()
        for path in ("/movies", "/series", "/channels", "/catalog/movies/detail?id=10", "/people?name=Bruce", "/missing"):
            with self.subTest(path=path):
                html = client.get(path).get_data(as_text=True)
                self.assertEqual(html.count('id="back-button"'), 1)
                self.assertIn('aria-label="Atrás"', html)
                header = re.search(r"<header.*?</header>", html, re.S).group(0)
                self.assertIn("<h1", header)
                self.assertIn('id="back-button"', header)
                self.assertIn("justify-content-between", header)
                self.assertIn('id="theme-icon" aria-hidden="true"', html)
                self.assertIn('/static/navigation.js', html)
                self.assertNotIn('Volver al listado', html)

    def test_all_results_over_100_in_web_api_people_and_cli(self):
        data = snapshot()
        for table, key in (("movies", "stream_id"), ("series", "series_id"), ("live_streams", "stream_id")):
            data[table] = [{key: i, "name": f"Record {i:03}", "cast": "Test Actor", "category_id": "1"} for i in range(125)]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        client.set_cookie("catalog_view", "table")
        for kind in ("movies", "series", "channels"):
            result = client.get(f"/api/catalog/{kind}?page=2&per_page=10").get_json()
            self.assertEqual(set(result), {"items", "total"})
            self.assertEqual(result["total"], 125)
            self.assertEqual(len(result["items"]), 125)
            html = client.get(f"/{kind}").get_data(as_text=True)
            self.assertEqual(html.count('<article class="catalog-card'), 50)
            self.assertIn("Página 1 de 3", html)
            self.assertNotIn('id="pagination"', html)
            self.assertNotIn('name="per_page"', html)
            descending = client.get(f"/api/catalog/{kind}?actor=test&sort=desc&sort_by=name").get_json()
            self.assertEqual(len(descending["items"]), 125)
            self.assertEqual(descending["items"][0]["name"], "Record 124")
        people = client.get("/people?name=Test%20Actor").get_data(as_text=True)
        self.assertNotIn("Record 124", people)
        self.assertEqual(people.count('<article class="catalog-card'), 100)
        self.assertIn("Record 000", people)
        last = client.get("/people?name=Test%20Actor&movies_page=3&series_page=3").get_data(as_text=True)
        self.assertIn("Record 124", last)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--db", str(self.path), "search", "Record"]), 0)
        self.assertEqual(output.getvalue().count("Record 124"), 3)

    def test_detail_field_labels_are_readable_in_spanish(self):
        data = snapshot()
        for table in ("movies", "series"):
            data[table][0].update({"cast": "Test Actor", "director": "Test Director", "plot": "Sinopsis de prueba", "episode_run_time": 45, "added": "1720531867", "new_provider_field": "dato"})
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind, record_id in (("movies", "10"), ("series", "20")):
            html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            for field, label in (("cast", "Reparto"), ("director", "Dirección"), ("plot", "Sinopsis"), ("category_name", "Categoría principal"), ("new_provider_field", "New provider field")):
                self.assertIn(f'data-field="{field}">{label}</th>', html)
            duration = "Duración por episodio (min)" if kind == "series" else "Duración (min)"
            self.assertIn(f'data-field="episode_run_time">{duration}</th>', html)

    def test_non_latin_catalog_is_separate_without_changing_records(self):
        from catalog import non_latin_title
        from html import unescape
        import re

        regular = ["El árbol", "The Batman", "Le cinéma français", "Coração português", "La città italiana", "Cafe\u0301", "Movie 2026 ★", "Москва transliterada".replace("Москва", "Moskva")]
        foreign = ["Москва", "한국 영화", "ภาพยนตร์", "日本語", "中文", "العربية", "Ελληνικά", "Movie 한국"]
        for title in regular:
            self.assertFalse(non_latin_title(title), title)
        for title in foreign:
            self.assertTrue(non_latin_title(title), title)
        data = snapshot()
        for table, key in (("movies", "stream_id"), ("series", "series_id")):
            data[table] = [{key: i, "name": title, "category_id": "1", "category_ids": [1], "cast": "Test Actor", "director": "Test Director"} for i, title in enumerate(regular + foreign)]
        data["live_streams"][0]["name"] = "한국 TV"
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind in ("movies", "series"):
            self.assertEqual(self.store.query(kind)["total"], len(regular + foreign))
            for scope, titles in ((0, regular), (1, foreign)):
                result = client.get(f"/api/catalog/{kind}", query_string={"foreign": scope, "actor": "test", "director": "DIRECTOR", "category": "1"}).get_json()
                self.assertEqual({item["name"] for item in result["items"]}, set(titles))
                html = client.get(f"/{kind}", query_string={"foreign": scope, "view": "table"}).get_data(as_text=True)
                self.assertEqual(html.count('<article class="catalog-card'), len(titles))
                links = [unescape(url) for url in re.findall(r'href="([^"]+)"', html)]
                record_link = next(url for url in links if f"/catalog/{kind}/detail?" in url)
                params = parse_qs(urlsplit(record_link).query)
                self.assertEqual(params["foreign"], [str(scope)])
                detail = client.get(record_link).get_data(as_text=True)
                back = unescape(re.search(r'id="back-button"[^>]*href="([^"]+)"', detail).group(1))
                self.assertEqual(parse_qs(urlsplit(back).query)["foreign"], [str(scope)])
                ordered = client.get(f"/{kind}?foreign={scope}&sort_by=rating&sort=desc")
                self.assertEqual(ordered.status_code, 200)
                self.assertEqual(ordered.get_data(as_text=True).count('<article class="catalog-card'), len(titles))
            self.assertEqual(client.get(f"/api/catalog/{kind}?foreign=invalid").status_code, 400)
        self.assertEqual(client.get("/api/catalog/channels").get_json()["total"], 2)
        people = client.get("/people?name=Test%20Actor").get_data(as_text=True)
        self.assertIn("한국 영화", people)
        self.assertIn("The Batman", people)
        self.assertEqual(self.store.detail("movies", "8")["data"], data["movies"][8])
        self.assertEqual(self.store.stats()["totals"]["movies"], len(regular + foreign))

    def test_detail_omits_category_list_only_when_identical_to_primary(self):
        client = create_app(self.path).test_client()
        for ids, show_list in (([1], False), (["1", 1], False), ([1, 2], True), ([2], True)):
            with self.subTest(ids=ids):
                data = snapshot()
                data["series"][0]["category_ids"] = ids
                self.store.replace(data)
                html = client.get("/catalog/series/detail?id=20").get_data(as_text=True)
                self.assertIn('data-field="category_name"', html)
                self.assertEqual('data-field="category_names"' in html, show_list)
                self.assertIn('class="category-link"', html)
                self.assertEqual(self.store.detail("series", "20")["data"], data["series"][0])

    def test_detail_combines_year_and_release_dates_in_one_row(self):
        client = create_app(self.path).test_client()
        for values, expected in (
            ({"year": "1995", "release_date": "1995-12-29"}, "1995 · 29/12/1995"),
            ({"year": "1995", "release_date": "1995-12-29", "releaseDate": "1995-12-29"}, "1995 · 29/12/1995"),
            ({"year": "1995"}, "1995"),
            ({"releaseDate": "1995-12-29"}, "29/12/1995"),
            ({"year": "1995", "release_date": "Sin confirmar"}, "1995 · Sin confirmar"),
            ({"year": "1995", "release_date": "1996-01-01"}, "1995 · 01/01/1996"),
            ({}, None),
        ):
            with self.subTest(values=values):
                data = snapshot()
                for key in ("year", "release_date", "releaseDate"):
                    data["movies"][0].pop(key, None)
                data["movies"][0].update(values)
                self.store.replace(data)
                html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
                fields = detail_fields(html)
                self.assertEqual(fields.get("premiere"), expected)
                for key in ("year", "release_date", "releaseDate"):
                    self.assertNotIn(key, fields)
                self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])

    def test_detail_ignores_provider_rating_scales_without_changing_source(self):
        client = create_app(self.path).test_client()
        for values, expected in (
            ({"rating": "8", "rating_5based": 4}, "8/10 · 4/5"),
            ({"rating": 7.6, "rating_5based": 3.8}, "7.6/10 · 3.8/5"),
            ({"rating": "8"}, "8/10"),
            ({"rating_5based": 4}, "4/5"),
            ({"rating": 0, "rating_5based": 0}, "0/10 · 0/5"),
            ({"rating": "", "rating_5based": None}, None),
        ):
            with self.subTest(values=values):
                data = snapshot()
                data["movies"][0].pop("rating", None)
                data["movies"][0].update(values)
                self.store.replace(data)
                html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
                fields = detail_fields(html)
                self.assertNotIn("rating", fields)
                self.assertNotIn("rating_5based", fields)
                self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])

    def test_detail_avoids_repeating_heading_and_shows_only_distinct_title(self):
        client = create_app(self.path).test_client()
        for name, title, heading, alternative in (
            ("A Beautiful Life (2023)", "A Beautiful Life", "A Beautiful Life (2023)", None),
            ("A Beautiful Life", "A Beautiful Life", "A Beautiful Life", None),
            (" A Beautiful Life (2023) ", "a beautiful   life", "A Beautiful Life (2023)", None),
            ("Una vida hermosa (2023)", "A Beautiful Life", "Una vida hermosa (2023)", "A Beautiful Life"),
            ("Movie", "", "Movie", None),
            ("", "Only Title", "Only Title", None),
            ("", "", "Sin título", None),
            ("1984 (1984)", "1984", "1984 (1984)", None),
            ("Film (Part 2)", "Film", "Film (Part 2)", "Film"),
        ):
            for kind, table, key in (("movies", "movies", "stream_id"), ("series", "series", "series_id"), ("channels", "live_streams", "stream_id")):
                with self.subTest(name=name, title=title, kind=kind):
                    data = snapshot()
                    data[table][0].update({"name": name, "title": title})
                    record_id = data[table][0][key]
                    self.store.replace(data)
                    html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
                    fields = detail_fields(html)
                    self.assertNotIn("name", fields)
                    self.assertNotIn("title", fields)
                    self.assertEqual(fields.get("alternative_title"), alternative)
                    self.assertIn(f'<h1 class="text-break mb-0">{heading}</h1>', html)
                    self.assertEqual(self.store.detail(kind, str(record_id))["data"], data[table][0])

    def test_backdrops_are_in_main_column_below_details_not_in_sidebar(self):
        import re

        data = snapshot()
        data["series"][0]["backdrop_path"] = ["https://example.invalid/background.jpg"]
        self.store.replace(data)
        html = create_app(self.path).test_client().get("/catalog/series/detail?id=20").get_data(as_text=True)
        sidebar = re.search(r'<aside.*?</aside>', html, re.S).group(0)
        main = re.search(r'<section.*?</section>', html, re.S).group(0)
        self.assertIn('id="detail-poster"', sidebar)
        self.assertNotIn('background.jpg', sidebar)
        self.assertIn('src="https://example.invalid/background.jpg"', main)
        self.assertIn('class="detail-backdrop rounded border bg-body-tertiary"', main)
        self.assertLess(main.index('</table>'), main.index('background.jpg'))
        self.assertIn('id="backdrop-error-1"', main)

    def test_detail_title_and_back_share_header_above_table(self):
        import re

        client = create_app(self.path).test_client()
        for kind, record_id in (("movies", 10), ("series", 20), ("channels", 1)):
            with self.subTest(kind=kind):
                html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
                main = re.search(r'<section.*?</section>', html, re.S).group(0)
                header = re.search(r'<header.*?</header>', main, re.S).group(0)
                self.assertIn("<h1", header)
                self.assertIn('id="back-button"', header)
                self.assertIn("justify-content-between", header)
                self.assertLess(header.index("<h1"), header.index('id="back-button"'))
                self.assertLess(main.index("</header>"), main.index("<table"))
                self.assertEqual(html.count('id="back-button"'), 1)

    def test_children_categories_hidden_and_deselected_when_excluded(self):
        import re

        data = snapshot()
        for table in ("live_categories", "vod_categories", "series_categories"):
            name = "S ANIME" if table == "series_categories" else "INFANTIL"
            data[table].append({"category_id": "90", "category_name": name})
        self.store.replace(data)
        client = create_app(self.path).test_client()
        for kind in ("movies", "series", "channels"):
            with self.subTest(kind=kind):
                url = f"/{kind}?children=exclude&category=90&view=cards"
                html = client.get(url).get_data(as_text=True)
                chip = re.search(r'<input[^>]*value="90"[^>]*>', html).group(0)
                self.assertIn("disabled", chip)
                self.assertNotIn("checked", chip)
                chip_id = re.search(r'id="([^"]+)"', chip).group(1)
                label = re.search(rf'<label[^>]*for="{chip_id}"[^>]*>', html).group(0)
                self.assertIn("hidden", label)
                result = client.get(f"/api/catalog/{kind}?children=exclude&category=90").json
                baseline = client.get(f"/api/catalog/{kind}?children=exclude").json
                self.assertEqual(result["total"], baseline["total"])
                self.assertGreater(result["total"], 0)
                html = client.get(f"/{kind}?children=all&category=90").get_data(as_text=True)
                chip = re.search(r'<input[^>]*value="90"[^>]*>', html).group(0)
                self.assertNotIn("disabled", chip)
                self.assertIn("checked", chip)
                kids = client.get(f"/kids/{kind}?category=90").get_data(as_text=True)
                chip = re.search(r'<input[^>]*value="90"[^>]*>', kids).group(0)
                self.assertNotIn("disabled", chip)
                self.assertIn("checked", chip)

    def test_detail_genre_and_categories_are_adjacent(self):
        import re

        client = create_app(self.path).test_client()
        for genre, categories in (("Drama", [1, 2]), ("Drama", [1]), ("", [1, 2])):
            data = snapshot()
            data["series"][0].update({"genre": genre, "category_ids": categories})
            self.store.replace(data)
            html = client.get("/catalog/series/detail?id=20").get_data(as_text=True)
            keys = re.findall(r'data-field="([^"]+)"', html)
            if genre:
                start = keys.index("genre")
                self.assertEqual(keys[start + 1], "category_name")
                if len(categories) > 1:
                    self.assertEqual(keys[start + 2], "category_names")
            else:
                self.assertIn("category_name", keys)
                self.assertIn("category_names", keys)

    def test_detail_youtube_id_becomes_clickable_video_link(self):
        client = create_app(self.path).test_client()
        for table, kind, record_id in (("movies", "movies", "10"), ("series", "series", "20")):
            data = snapshot()
            data[table][0]["youtube_trailer"] = "nsuk8ThvnpM"
            self.store.replace(data)
            html = client.get(f"/catalog/{kind}/detail?id={record_id}").get_data(as_text=True)
            self.assertIn('href="https://www.youtube.com/watch?v=nsuk8ThvnpM"', html)
            self.assertIn('rel="noopener noreferrer"', html)
            self.assertIn("Ver tráiler en YouTube", html)
            self.assertIn('class="trailer-icon"', html)
            self.assertIn('src="/static/youtube.svg"', html)
            self.assertNotIn('>Ver tráiler en YouTube</a>', html)
            self.assertNotIn('>"nsuk8ThvnpM"</pre>', html)
            self.assertEqual(self.store.detail(kind, record_id)["data"], data[table][0])
        for value in ("", None, "javascript:alert(1)"):
            data = snapshot()
            data["movies"][0]["youtube_trailer"] = value
            self.store.replace(data)
            html = client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
            self.assertNotIn('class="trailer-icon"', html)

    def test_cards_hide_trailer_icons_but_keep_trailer_api_data(self):
        import re

        data = snapshot()
        for table in ("movies", "series", "live_streams"):
            data[table][0].update({
                "youtube_trailer": " nsuk8ThvnpM ",
                "cast": "Test Person",
            })
        self.store.replace(data)
        client = create_app(self.path).test_client()
        client.set_cookie("catalog_view", "table")
        for kind, record_id in (("movies", "10"), ("series", "20"), ("channels", "1")):
            with self.subTest(kind=kind):
                html = client.get(f"/{kind}?q=Batman").get_data(as_text=True)
                card = re.search(r'<article class="catalog-card.*?</article>', html, re.S)[0]
                self.assertNotIn("<table", html)
                self.assertNotIn('class="trailer-icon"', card)
                self.assertNotIn('src="/static/youtube.svg"', card)
                item = client.get(f"/api/catalog/{kind}?q=Batman&sort=desc").get_json()["items"][0]
                self.assertEqual(item["id"], record_id)
                self.assertEqual(item["trailer_url"], "https://www.youtube.com/watch?v=nsuk8ThvnpM")
                self.assertNotIn("youtube_trailer", item)
                self.assertNotIn("raw_json", item)
                self.assertEqual(self.store.detail(kind, record_id)["data"]["youtube_trailer"], " nsuk8ThvnpM ")
        people = client.get("/people?name=Test+Person").get_data(as_text=True)
        self.assertNotIn('class="trailer-icon"', people)
        with client.get("/static/youtube.svg") as icon:
            self.assertEqual(icon.status_code, 200)
        data["movies"][0]["name"] = "Бэтмен"
        self.store.replace(data)
        foreign = client.get("/movies?foreign=1").get_data(as_text=True)
        self.assertNotIn('class="trailer-icon"', foreign)
        self.assertNotIn('class="trailer-icon"', client.get("/movies").get_data(as_text=True))

    def test_listing_without_valid_trailer_has_empty_icon_cell(self):
        client = create_app(self.path).test_client()
        client.set_cookie("catalog_view", "table")
        for value in ("", None, False, 123, [], {}, "short", "javascript:alert(1)", '"><script>alert(1)</script>'):
            with self.subTest(value=value):
                data = snapshot()
                data["movies"][0]["youtube_trailer"] = value
                self.store.replace(data)
                html = client.get("/movies?q=Batman").get_data(as_text=True)
                self.assertIn('<article class="catalog-card', html)
                self.assertNotIn('class="trailer-icon"', html)
                item = client.get("/api/catalog/movies?q=Batman").get_json()["items"][0]
                self.assertIsNone(item["trailer_url"])
        self.assertIn('No hay resultados.', client.get("/movies?q=NoSuchMovie").get_data(as_text=True))
        data = snapshot()
        data["movies"][0].update({"name": '<script>"title"</script>', "youtube_trailer": "nsuk8ThvnpM"})
        self.store.replace(data)
        html = client.get("/movies").get_data(as_text=True)
        self.assertIn('aria-label="Ver tráiler de &lt;script&gt;&#34;title&#34;&lt;/script&gt; en YouTube"', html)
        self.assertNotIn('<script>"title"</script>', html)

    def test_card_posters_use_valid_images_with_lazy_loading(self):
        data = snapshot()
        data["movies"][0].update({
            "stream_icon": "https://example.invalid/poster.jpg",
            "cast": "Test Person",
        })
        data["series"][0]["cast"] = "Test Person"
        self.store.replace(data)
        client = create_app(self.path).test_client()
        client.set_cookie("catalog_view", "table")
        for kind, expected in (
            ("movies", "https://example.invalid/poster.jpg"),
            ("series", "https://example.invalid/cover.png"),
        ):
            with self.subTest(kind=kind):
                html = client.get(f"/{kind}?q=Batman").get_data(as_text=True)
                self.assertIn(f'src="{expected}"', html)
                self.assertIn('loading="lazy" referrerpolicy="no-referrer"', html)
                self.assertNotIn('/static/preview.js', html)
                self.assertEqual(
                    client.get(f"/api/catalog/{kind}?q=Batman").get_json()["items"][0]["preview_url"],
                    expected,
                )
                self.assertIn(
                    "img-src 'self' data: http: https:",
                    client.get(f"/{kind}").headers["Content-Security-Policy"],
                )
        self.assertNotIn('data-preview-url=', client.get("/channels").get_data(as_text=True))
        self.assertIsNone(client.get("/api/catalog/channels?q=Batman").get_json()["items"][0]["preview_url"])
        people = client.get("/people?name=Test+Person").get_data(as_text=True)
        self.assertEqual(people.count('class="catalog-poster"'), 2)
        self.assertNotIn('/static/preview.js', people)

    def test_preview_images_reject_unsafe_urls_and_use_cover_fallback(self):
        client = create_app(self.path).test_client()
        for value in (None, "", [], {}, "javascript:alert(1)", "file:///tmp/poster",
                      "https://user:secret@example.invalid/image", "http://[invalid"):
            with self.subTest(value=value):
                data = snapshot()
                data["movies"][0]["stream_icon"] = value
                self.store.replace(data)
                self.assertIsNone(client.get("/api/catalog/movies?q=Batman").get_json()["items"][0]["preview_url"])
                self.assertNotIn('data-preview-url=', client.get("/movies?q=Batman").get_data(as_text=True))
                data["movies"][0]["cover"] = "https://example.invalid/fallback.jpg"
                self.store.replace(data)
                self.assertEqual(
                    client.get("/api/catalog/movies?q=Batman").get_json()["items"][0]["preview_url"],
                    "https://example.invalid/fallback.jpg",
                )
                self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])

    def test_children_catalog_and_toggle_use_primary_and_secondary_categories(self):
        data = snapshot()
        for table in ("live_categories", "vod_categories", "series_categories"):
            name = "S INFANTIL" if table == "series_categories" else "INFANTIL"
            data[table].append({"category_id": "99", "category_name": name})
        for table in ("movies", "series", "live_streams"):
            data[table][0]["category_ids"] = [1, 99, "99"]
        data["vod_categories"].extend([
            {"category_id": "98", "category_name": "ANIME"},
            {"category_id": "97", "category_name": "CAST. ANIMACION"},
        ])
        data["movies"][1]["category_id"] = "98"
        data["movies"][2]["category_id"] = "97"
        data["series"][0]["name"] = "Дети"
        self.store.replace(data)
        for kind, expected in (("movies", 3), ("series", 1), ("channels", 1)):
            with self.subTest(kind=kind):
                client = create_app(self.path).test_client()
                all_items = self.store.query(kind)["total"]
                self.assertEqual(self.store.query(kind, children="only")["total"], expected)
                self.assertEqual(self.store.query(kind, children="exclude")["total"], all_items - expected)
                self.assertEqual(client.get(f"/api/catalog/{kind}?children=only").get_json()["total"], expected)
                html = client.get(f"/kids/{kind}").get_data(as_text=True)
                self.assertIn("Niños ·", html)
                self.assertIn('value="only"', html)
                self.assertNotIn('id="children-toggle"', html)
                self.assertIn("children=only", html)
                self.assertNotIn("control parental", html)
        client = create_app(self.path).test_client()
        self.assertEqual(client.get("/api/catalog/movies").get_json()["total"], 1)
        html = client.get("/movies").get_data(as_text=True)
        self.assertIn('id="children-toggle"', html)
        self.assertNotRegex(html, r'id="children-toggle"[^>]*\bchecked')
        response = client.get("/api/catalog/movies?children=all")
        self.assertEqual(response.get_json()["total"], 4)
        self.assertIn("children_visibility=all", response.headers["Set-Cookie"])
        self.assertEqual(client.get("/api/catalog/movies").get_json()["total"], 4)
        self.assertRegex(client.get("/series").get_data(as_text=True), r'id="children-toggle"[^>]*\bchecked')
        client.get("/kids/movies")
        self.assertEqual(client.get("/api/catalog/movies").get_json()["total"], 4)
        self.assertEqual(client.get("/api/catalog/movies?children=exclude").get_json()["total"], 1)
        self.assertEqual(client.get("/api/catalog/channels").get_json()["total"], 1)
        self.assertEqual(client.get("/movies?children=exclude&children=all").status_code, 200)
        self.assertRegex(client.get("/movies").get_data(as_text=True), r'id="children-toggle"[^>]*\bchecked')
        self.assertEqual(client.get("/kids/invalid").status_code, 404)
        self.assertEqual(client.get("/api/catalog/movies?children=invalid").status_code, 400)
        detail = client.get("/catalog/movies/detail?id=10&children=only").get_data(as_text=True)
        self.assertIn('/kids/movies?', detail)
        self.assertEqual(self.store.detail("movies", "10")["data"], data["movies"][0])

    def test_children_filters_combine_with_search_people_category_and_sort(self):
        data = snapshot()
        data["vod_categories"].append({"category_id": "99", "category_name": "animacion"})
        data["movies"][0].update(category_ids=["99"], cast="Test Person", director="Test Director")
        data["movies"][1].update(category_id="99", year="2025")
        self.store.replace(data)
        client = create_app(self.path).test_client()
        items = client.get("/api/catalog/movies?children=only&category=99&q=bat&actor=Test&director=Director").get_json()["items"]
        self.assertEqual([item["id"] for item in items], ["10"])
        items = client.get("/api/catalog/movies?children=only&sort_by=year&sort=desc").get_json()["items"]
        self.assertEqual([item["id"] for item in items], ["11", "10"])
        self.assertEqual(
            client.get("/api/catalog/movies?children=exclude&category=99").get_json()["total"],
            client.get("/api/catalog/movies?children=exclude").get_json()["total"],
        )
        self.assertEqual(self.store.query("movies", person="Test Person")["total"], 1)

    def test_removed_dashboard_redirects_to_movies_and_is_absent_from_navigation(self):
        client = create_app(self.path).test_client()
        for path in ("/", "/dashboard", "/dashboard?sort_by=total"):
            response = client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/movies")
        html = client.get("/movies").get_data(as_text=True)
        self.assertNotIn('href="/dashboard"', html)
        self.assertNotIn(">Resumen</a>", html)
        self.assertIn('class="navbar-brand" href="/movies"', html)
        self.assertEqual(client.get("/missing").status_code, 404)
        self.assertIn("Volver a películas", client.get("/missing").get_data(as_text=True))

    def test_legacy_table_preference_and_urls_always_render_paginated_cards(self):
        data = snapshot()
        for table, key in (("movies", "stream_id"), ("series", "series_id"), ("live_streams", "stream_id")):
            data[table] = [{key: i, "name": f"Record {i:03}", "cast": "Test Person"} for i in range(60)]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        client.set_cookie("catalog_view", "table")
        response = client.get("/movies?view=table&page=2")
        self.assertIn("catalog_view=;", response.headers.get("Set-Cookie", ""))
        for path in ("/movies", "/series", "/channels"):
            html = client.get(path + "?view=table&page=2").get_data(as_text=True)
            self.assertEqual(html.count('<article class="catalog-card'), 10)
            self.assertIn("Página 2 de 2", html)
            self.assertIn('name="view" value="cards"', html)
            self.assertNotIn("<table", html)
            self.assertNotIn('<select id="view"', html)
        html = client.get("/people?name=Test+Person&view=table&movies_page=2&series_page=2").get_data(as_text=True)
        self.assertEqual(html.count('<article class="catalog-card'), 20)
        self.assertNotIn("<table", html)
        self.assertNotIn('aria-label="Vista del listado"', html)
        for path in ("/kids/movies", "/movies?foreign=1"):
            html = client.get(path).get_data(as_text=True)
            self.assertNotIn("control parental", html)
            self.assertNotIn("traducción al español pendiente", html)

    def test_cards_paginate_globally_and_legacy_api_stays_complete(self):
        data = snapshot()
        for table, key in (("movies", "stream_id"), ("series", "series_id"), ("live_streams", "stream_id")):
            data[table] = [
                {key: n, "name": f"Record {n:03}", "category_id": "1", "year": str(1900 + n),
                 "rating": n % 10, "stream_icon": "https://example.invalid/poster.jpg",
                 "youtube_trailer": "nsuk8ThvnpM", "cast": "Test Person"}
                for n in range(125)
            ]
        self.store.replace(data)
        for kind in ("movies", "series", "channels"):
            with self.subTest(kind=kind):
                client = create_app(self.path).test_client()
                result = self.store.query(kind, page=2)
                self.assertEqual(result["total"], 125)
                self.assertEqual(result["page"], 2)
                self.assertEqual(result["pages"], 3)
                self.assertEqual(result["per_page"], 50)
                self.assertEqual(len(result["items"]), 50)
                self.assertEqual(result["items"][0]["name"], "Record 050")
                api = client.get(f"/api/catalog/{kind}?view=cards&page=2").get_json()
                self.assertEqual(api["total"], 125)
                self.assertEqual(len(api["items"]), 50)
                self.assertEqual(api["html"].count('<article class="catalog-card'), 50)
                self.assertIn("Página 2 de 3", api["html"])
                self.assertIn('loading="lazy" referrerpolicy="no-referrer"', api["html"])
                self.assertNotIn('class="trailer-icon"', api["html"])
                html = client.get(f"/{kind}?view=cards&page=6").get_data(as_text=True)
                self.assertEqual(html.count('<article class="catalog-card'), 25)
                self.assertNotIn('id="table-results"', html)
                final = client.get(f"/api/catalog/{kind}?view=cards&page=99999").get_json()
                self.assertEqual(final["page"], 3)
                desc = client.get(f"/api/catalog/{kind}?view=cards&sort_by=year&sort=desc&page=2").get_json()
                self.assertEqual(desc["items"][0]["name"], "Record 074")
                table = client.get(f"/api/catalog/{kind}?view=table&page=6").get_json()
                self.assertEqual(len(table["items"]), 125)
                self.assertEqual(set(table), {"items", "total"})
                html = client.get(f"/{kind}?view=table").get_data(as_text=True)
                self.assertEqual(html.count('<article class="catalog-card'), 50)
                self.assertIn('src="https://example.invalid/poster.jpg"', html)
        self.assertEqual(len(self.store.query("movies")["items"]), 125)

    def test_card_filters_validation_preferences_and_detail_return(self):
        data = snapshot()
        data["vod_categories"].append({"category_id": "99", "category_name": "INFANTIL"})
        data["movies"] = [
            {"stream_id": n, "name": f"Kids {n:03}", "category_id": "99",
             "cast": "Test Person", "director": "Test Director"}
            for n in range(100)
        ]
        data["movies"].append({"stream_id": 100, "name": "Бэтмен", "category_id": "2"})
        self.store.replace(data)
        client = create_app(self.path).test_client()
        html = client.get("/kids/movies?view=cards&page=2").get_data(as_text=True)
        self.assertEqual(html.count('<article class="catalog-card'), 50)
        self.assertIn('/kids/movies?', html)
        self.assertIn('page=2', html)
        self.assertIn("Sin portada", html)
        self.assertIn('name="view" value="cards"', client.get("/series").get_data(as_text=True))
        self.assertEqual(client.get("/api/catalog/movies?children=only&view=cards&q=Kids+049&actor=Test&director=Director&category=99&page=2").get_json()["page"], 1)
        foreign = client.get("/movies?foreign=1&view=cards").get_data(as_text=True)
        self.assertIn("Бэтмен", foreign)
        self.assertEqual(foreign.count('<article class="catalog-card'), 1)
        detail = client.get("/catalog/movies/detail?id=30&view=cards&page=2&children=only&q=Kids").get_data(as_text=True)
        from html import unescape
        import re
        href = unescape(re.search(r'id="back-button"[^>]*href="([^"]+)"', detail)[1])
        self.assertEqual(urlsplit(href).path, "/kids/movies")
        query = parse_qs(urlsplit(href).query)
        self.assertEqual(query["view"], ["cards"])
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["q"], ["Kids"])
        empty = client.get("/api/catalog/movies?view=cards&q=Absent").get_json()
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["page"], 1)
        self.assertEqual(empty["items"], [])
        for args in ("view=invalid", "view=cards&page=0", "view=cards&page=-1",
                     "view=cards&page=abc", "view=cards&page=9999999999999999"):
            self.assertEqual(client.get("/api/catalog/movies?" + args).status_code, 400)
            self.assertEqual(client.get("/movies?" + args).status_code, 400)
        client.get("/movies?view=table")
        self.assertIn('name="view" value="cards"', client.get("/channels").get_data(as_text=True))

    def test_people_cards_have_independent_pages_and_return_context(self):
        data = snapshot()
        for table, key in (("movies", "stream_id"), ("series", "series_id")):
            data[table] = [
                {key: n, "name": f"Title {n:03}", "cast": "Test Person", "category_id": "1"}
                for n in range(125)
            ]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        html = client.get("/people?name=Test+Person&view=cards&movies_page=2&series_page=3").get_data(as_text=True)
        self.assertEqual(html.count('<article class="catalog-card'), 75)
        self.assertIn("Página 2 de 3", html)
        self.assertIn("Página 3 de 3", html)
        detail = client.get("/catalog/movies/detail?id=30&view=cards&person=Test+Person&movies_page=2&series_page=3").get_data(as_text=True)
        self.assertIn('/people?name=Test+Person&amp;view=cards&amp;movies_page=2&amp;series_page=3', detail)
        table = client.get("/people?name=Test+Person&view=table").get_data(as_text=True)
        self.assertEqual(table.count('<article class="catalog-card'), 100)
        self.assertNotIn('<table', table)

    def test_cards_are_default_and_filters_live_in_sidebar(self):
        client = create_app(self.path).test_client()
        for url in ("/movies", "/series", "/channels", "/kids/movies", "/movies?foreign=1"):
            html = client.get(url).get_data(as_text=True)
            self.assertIn('name="view" value="cards"', html)
            self.assertNotIn('<select id="view"', html)
            self.assertIn('aria-label="Filtros del catálogo"', html)
            self.assertIn('class="d-flex flex-column gap-3"', html)
            self.assertLess(html.index('<aside class="catalog-sidebar'), html.index('<form id="filters"'))
            self.assertLess(html.index('</aside>'), html.index('<section class="catalog-content"'))
        people = client.get("/people?name=Nobody").get_data(as_text=True)
        self.assertNotIn('aria-label="Vista del listado"', people)
        self.assertEqual(set(client.get("/api/catalog/movies").get_json()), {"items", "total"})
        client.get("/movies?view=table")
        self.assertIn('name="view" value="cards"', client.get("/series").get_data(as_text=True))
        self.assertIn('name="view" value="cards"', client.get("/series?view=cards").get_data(as_text=True))

    def test_cards_escape_titles_and_reject_unsafe_poster_urls(self):
        data = snapshot()
        data["movies"][0].update({
            "name": '<script>alert("title")</script>',
            "stream_icon": 'javascript:alert("image")',
        })
        self.store.replace(data)
        client = create_app(self.path).test_client()
        html = client.get("/movies?view=cards").get_data(as_text=True)
        self.assertIn('&lt;script&gt;alert(&#34;title&#34;)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert("title")</script>', html)
        self.assertNotIn('javascript:', html)
        self.assertIn("Sin portada", html)
        for page in (0, -1, "2", True):
            with self.assertRaises(ValueError):
                self.store.query("movies", page=page)

    def test_category_and_genre_chips_filter_with_or_within_and_between_groups(self):
        data = snapshot()
        data["movies"] = [
            {"stream_id": 1, "name": "One", "category_id": "1", "category_ids": [1, 2, 2], "genre": "Drama, Science Fiction", "cast": "Test Person"},
            {"stream_id": 2, "name": "Two", "category_id": "2", "genre": "Comedy, Drama"},
            {"stream_id": 3, "name": "Three", "category_id": "404", "genre": "Dramatic, Comedy"},
            {"stream_id": 4, "name": "Four", "category_id": "1", "genre": "Drama"},
        ]
        self.store.replace(data)
        self.assertEqual(set(self.store.genres("movies")), {"Drama", "Science Fiction", "Comedy", "Dramatic"})
        self.assertEqual(self.store.query("movies", category=["1", "2"])["total"], 3)
        self.assertEqual(self.store.query("movies", category=["2"])["total"], 2)
        self.assertEqual(self.store.query("movies", category=["1", "__uncategorized__"])["total"], 3)
        self.assertEqual(self.store.query("movies", genre=["drama"])["total"], 3)
        self.assertEqual(self.store.query("movies", genre=["Science Fiction", "Comedy"])["total"], 3)
        query = [("category", "1"), ("category", "2"), ("genre", "Science Fiction"), ("genre", "Comedy"),
                 ("view", "table"), ("sort", "desc"), ("sort_by", "name")]
        client = create_app(self.path).test_client()
        result = client.get("/api/catalog/movies", query_string=query).get_json()
        self.assertEqual([item["name"] for item in result["items"]], ["Two", "One"])
        html = client.get("/movies", query_string=query).get_data(as_text=True)
        self.assertIn('<legend class="fs-6">Categorías</legend>', html)
        self.assertIn('<legend class="fs-6">Géneros</legend>', html)
        self.assertIn('name="category" value="1" checked', html)
        self.assertIn('name="category" value="2" checked', html)
        self.assertIn('name="genre" value="Comedy" checked', html)
        self.assertNotIn('<select id="category"', html)
        self.assertEqual(client.get("/api/catalog/movies?genre=Dram").get_json()["total"], 0)
        self.assertEqual(client.get("/api/catalog/movies?genre=%25").get_json()["total"], 0)
        self.assertEqual(client.get("/api/catalog/movies?genre=" + "a" * 201).status_code, 400)
        self.assertEqual(client.get("/api/catalog/movies", query_string=[("category", str(i)) for i in range(101)]).status_code, 400)
        filtered = client.get("/api/catalog/movies", query_string=query + [("actor", "Test")]).get_json()
        self.assertEqual([item["id"] for item in filtered["items"]], ["1"])

    def test_chip_selections_survive_cards_pagination_sorting_and_detail_back(self):
        from html import unescape
        import re

        data = snapshot()
        data["movies"] = [
            {"stream_id": n, "name": f"Title {n:03}", "category_id": "1", "genre": "Drama"}
            for n in range(60)
        ]
        self.store.replace(data)
        client = create_app(self.path).test_client()
        query = [("view", "cards"), ("category", "1"), ("category", "2"),
                 ("genre", "Drama"), ("genre", "Comedy"), ("page", "2")]
        html = client.get("/movies", query_string=query).get_data(as_text=True)
        self.assertEqual(html.count('<article class="catalog-card'), 10)
        links = re.findall(r'href="([^"]+)"', html)
        for url in links:
            parsed = urlsplit(unescape(url))
            if parsed.path.startswith("/catalog/movies/detail") or "sort_by=" in parsed.query:
                params = parse_qs(parsed.query)
                self.assertEqual(params["category"], ["1", "2"])
                self.assertEqual(params["genre"], ["Drama", "Comedy"])
        detail = client.get("/catalog/movies/detail", query_string=query + [("id", "55")]).get_data(as_text=True)
        back = unescape(re.search(r'id="back-button"[^>]*href="([^"]+)"', detail)[1])
        self.assertEqual(parse_qs(urlsplit(back).query)["genre"], ["Drama", "Comedy"])
        self.assertEqual(parse_qs(urlsplit(back).query)["category"], ["1", "2"])
        self.assertEqual(client.get(back).status_code, 200)
        result = client.get("/api/catalog/movies", query_string=query).get_json()
        self.assertEqual(len(result["items"]), 10)
        self.assertIn("genre=Drama&amp;genre=Comedy", result["html"])

    def test_stats_include_unknown_categories(self):
        stats = self.store.stats()
        self.assertEqual(stats["totals"], {"channels": 2, "movies": 4, "series": 1})
        self.assertTrue(any(item["category"] == "Sin categoría" for item in stats["largest"]))
        self.assertEqual(stats["largest"][0]["total"], 2)

    def test_sync_updates_and_preserves_rows_missing_from_download(self):
        changed = copy.deepcopy(self.data)
        changed["movies"] = [{"stream_id": 10, "name": "Batman actualizado", "category_id": "1"}]
        client = Mock()
        client.download.return_value = changed
        counts = self.store.sync(client)
        self.assertEqual(counts["movies"], 4)
        self.assertEqual(self.store.detail("movies", "10")["name"], "Batman actualizado")
        changed["movies"] = []
        self.store.sync(client)
        self.assertEqual(self.store.stats()["totals"]["movies"], 4)

    def test_duplicate_ids_keep_last_record_and_are_reported(self):
        changed = copy.deepcopy(self.data)
        changed["vod_categories"].append({"category_id": "1", "category_name": "Acción 2"})
        changed["movies"].append({"stream_id": 10, "name": "Batman duplicado", "category_id": "1"})
        client = Mock()
        client.download.return_value = changed
        self.store.sync(client)
        self.assertEqual(self.store.duplicates, {"vod_categories": 1, "movies": 1})
        self.assertEqual(self.store.stats()["totals"]["movies"], 4)
        self.assertEqual(self.store.query("movies", q="duplicado")["total"], 1)

    def test_invalid_snapshot_rolls_back(self):
        before = self.store.stats()
        for bad in (None, {}, [{"name": "Sin identificador"}], [False]):
            with self.subTest(bad=bad):
                changed = copy.deepcopy(self.data)
                changed["series"] = bad
                with self.assertRaises(CatalogError):
                    self.store.replace(changed)
                self.assertEqual(self.store.stats(), before)
        with self.assertRaises(CatalogError):
            self.store.replace({})

    def test_download_failure_leaves_database_unchanged(self):
        before = self.store.stats()
        client = Mock()
        client.download.side_effect = CatalogError("API no disponible")
        with self.assertRaises(CatalogError):
            self.store.sync(client)
        self.assertEqual(self.store.stats(), before)

    def test_csv_exports_all_original_fields_and_empty_headers(self):
        destination = Path(self.temp.name) / "movies.csv"
        self.assertEqual(self.store.export("movies", destination), 4)
        frame = pd.read_csv(destination)
        self.assertIn("unknown.nested", frame.columns)
        self.assertIn("tags", frame.columns)
        self.assertNotIn("Unnamed: 0", frame.columns)
        self.assertEqual(frame.loc[frame["name"] == "Batman", "unknown.nested"].iloc[0], "preservado")
        self.assertTrue(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
        series_csv = Path(self.temp.name) / "series.csv"
        self.assertEqual(self.store.export("series", series_csv), 1)
        self.assertIn("seasons", pd.read_csv(series_csv).columns)
        self.store.replace({table: [] for table in ACTIONS})
        self.assertEqual(self.store.export("movies", destination), 0)
        self.assertEqual(list(pd.read_csv(destination).columns), ["stream_id", "name", "category_id"])

    def test_cli_stats_search_export_and_invalid_input(self):
        for command, expected in [
            (["stats"], "Películas: 4"),
            (["search", "batman"], "Batman: la serie"),
            (["export", str(Path(self.temp.name) / "movies.csv")], "Exportados 4"),
        ]:
            with self.subTest(command=command), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--db", str(self.path), *command]), 0)
                self.assertIn(expected, output.getvalue())
        for command in (["search", " "], ["export", "other.csv"], ["serve", "--port", "0"]):
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["--db", str(self.path), *command]), 1)
                self.assertIn("Error:", errors.getvalue())

    def test_flask_pages_and_escaped_api_names(self):
        client = create_app(self.path).test_client()
        self.assertEqual(client.get("/").status_code, 302)
        self.assertEqual(client.get("/dashboard").location, "/movies")
        for path in ("/movies", "/series", "/channels"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Cat", response.data)
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertIn("img-src 'self' data:", response.headers["Content-Security-Policy"])
                self.assertIn(b'id="theme-toggle"', response.data)
                self.assertIn(b'/static/theme.js', response.data)
                self.assertNotIn(b'bg-white', response.data)
                self.assertNotIn(b'table-light', response.data)
        response = client.get("/movies")
        self.assertIn(b"&lt;script&gt;", response.data)
        self.assertNotIn(b"<script>alert(1)</script>", response.data)
        result = client.get("/api/catalog/movies?q=batman&category=1&per_page=1").get_json()
        self.assertEqual(set(result), {"items", "total"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["name"], "Batman")
        self.assertNotIn("raw_json", result["items"][0])
        self.assertNotIn("icon", result["items"][0])

    def test_flask_validation_and_empty_state(self):
        client = create_app(self.path).test_client()
        for query in ("sort=invalid", "q=" + "a" * 201):
            with self.subTest(query=query):
                response = client.get("/api/catalog/movies?" + query)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(client.get("/movies?" + query).status_code, 400)
        self.assertEqual(client.get("/api/catalog/missing").status_code, 404)
        self.assertEqual(client.get("/missing").status_code, 404)
        self.store.replace({table: [] for table in ACTIONS})
        self.assertIn("No hay resultados", client.get("/movies").get_data(as_text=True))
        self.assertEqual(client.get("/api/catalog/series").get_json(), {"items": [], "total": 0})


class FakeXtreamHandler(BaseHTTPRequestHandler):
    received = []
    data = snapshot()
    status = 200

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        self.received.append((urlsplit(self.path).path, query))
        if self.status != 200:
            self.send_response(self.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if query.get("username") != ["demo"] or query.get("password") != ["test-password"]:
            payload = {"user_info": {"auth": 0}}
        else:
            action = query.get("action", [""])[0]
            table = next((table for table, value in ACTIONS.items() if value == action), None)
            payload = self.data.get(table, {})
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeXtreamHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/base"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        FakeXtreamHandler.received.clear()
        self.config_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_temp.cleanup)
        self.config_path = Path(self.config_temp.name) / "config.local.json"
        config_patch = patch("xtream.CONFIG_PATH", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.client = XtreamClient(self.url, "demo", "test-password")
        self.addCleanup(self.client.close)

    def test_real_http_sync_uses_six_actions_and_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CatalogStore(Path(folder) / "test.db")
            result = store.sync(self.client)
            self.assertEqual(result["movies"], 4)
            self.assertEqual(store.stats()["totals"]["series"], 1)
        self.assertEqual(len(FakeXtreamHandler.received), 6)
        self.assertEqual(
            {params["action"][0] for _, params in FakeXtreamHandler.received},
            set(ACTIONS.values()),
        )
        for path, params in FakeXtreamHandler.received:
            self.assertEqual(path, "/base/player_api.php")
            self.assertEqual(params["username"], ["demo"])
            self.assertEqual(params["password"], ["test-password"])

    def test_cli_sync_with_environment_and_real_http(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {
                "XTREAM_SERVER_URL": self.url + "/player_api.php",
                "XTREAM_USERNAME": "demo",
                "XTREAM_PASSWORD": "test-password",
            }
            with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--db", str(Path(folder) / "cli.db"), "sync"]), 0)
            self.assertIn("Sincronización completada", output.getvalue())
            self.assertNotIn("test-password", output.getvalue())

    def test_retries_preserve_http_status_and_existing_catalog(self):
        adapter = self.client.session.get_adapter(self.url)
        adapter.max_retries = adapter.max_retries.new(backoff_factor=0)
        with tempfile.TemporaryDirectory() as folder:
            store = CatalogStore(Path(folder) / "existing.db")
            store.replace(snapshot())
            before = store.stats()
            with patch.object(FakeXtreamHandler, "status", 503):
                with self.assertRaisesRegex(CatalogError, "HTTP 503.*Servicio no disponible") as caught:
                    store.sync(self.client)
            self.assertEqual(store.stats(), before)
            self.assertEqual(len(FakeXtreamHandler.received), 3)
            self.assertNotIn("test-password", str(caught.exception))

    def test_auth_failure_is_explicit_and_redacted(self):
        client = XtreamClient(self.url, "demo", "wrong")
        self.addCleanup(client.close)
        with self.assertRaisesRegex(CatalogError, "credenciales") as caught:
            client.download()
        self.assertNotIn("wrong", str(caught.exception))

    def test_cli_sync_with_local_config(self):
        self.config_path.write_text(json.dumps({
            "server_url": self.url, "username": "demo", "password": "test-password",
        }), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--db", str(self.config_path.parent / "local.db"), "sync"]), 0)
        self.assertIn("Sincronización completada", output.getvalue())
        self.assertNotIn("test-password", output.getvalue())

    def test_environment_overrides_local_config_per_field(self):
        self.config_path.write_text(json.dumps({
            "server_url": self.url, "username": "local", "password": "local-password",
        }), encoding="utf-8")
        with patch.dict(os.environ, {"XTREAM_USERNAME": "demo"}, clear=True):
            client = XtreamClient.from_env()
        self.addCleanup(client.close)
        self.assertEqual(client.credentials, {"username": "demo", "password": "local-password"})

    def test_invalid_local_config_is_explicit_without_leaking_values(self):
        for content in ('{"password":"private-value"', "[]", '{"password":123}', '{"unknown":"private-value"}'):
            with self.subTest(content=content):
                self.config_path.write_text(content, encoding="utf-8")
                with patch.dict(os.environ, {}, clear=True), self.assertRaises(CatalogError) as caught:
                    XtreamClient.from_env()
                self.assertNotIn("private-value", str(caught.exception))
        self.config_path.write_bytes(b"\xff")
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(CatalogError, "UTF-8"):
            XtreamClient.from_env()

    def test_invalid_config_rejected(self):
        for url in ("ftp://example.com", "https://u:p@example.com", "https://example.com?x=y", "invalid", "http://example.com:invalid"):
            with self.subTest(url=url), self.assertRaises(CatalogError):
                XtreamClient(url, "u", "p")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(CatalogError):
            XtreamClient.from_env()

    def test_http_json_and_network_errors_are_redacted(self):
        for status, payload in ((403, []), (302, []), (200, {"error": "secret"}), (200, [1])):
            response = Mock()
            response.status_code = status
            response.json.return_value = payload
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with self.subTest(status=status, payload=payload):
                with patch.object(self.client.session, "get", return_value=response):
                    with self.assertRaises(CatalogError) as caught:
                        self.client.fetch("get_series")
                    self.assertNotIn("secret", str(caught.exception))
        for error in (ValueError("JSON inválido con secretos"), requests.exceptions.JSONDecodeError("Invalid JSON", "x", 0)):
            response.json.side_effect = error
            with patch.object(self.client.session, "get", return_value=response):
                with self.assertRaisesRegex(CatalogError, "JSON válido"):
                    self.client.fetch("get_series")
        for error, message in (
            (requests.Timeout("test-password"), "tiempo de espera"),
            (requests.exceptions.SSLError("test-password"), "fallo TLS"),
            (requests.ConnectionError("test-password"), "fallo de conexión"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.client.session, "get", side_effect=error):
                    with self.assertRaisesRegex(CatalogError, message) as caught:
                        self.client.fetch("get_series")
                    self.assertNotIn("test-password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
