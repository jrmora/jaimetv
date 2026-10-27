import copy
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from app import create_app
from catalog import CatalogStore, fingerprint
from provider_info import normalize_provider_info, provider_fingerprint, provider_diagnostic, provider_presentation
from test_app import snapshot
from xtream import CatalogError, XtreamClient


def movie_info():
    return {
        "info": {
            "name": "Ice Age", "duration": "01:28:00", "duration_secs": 5280, "bitrate": 1400,
            "audio": [
                {"codec_name": "aac", "channels": 2, "tags": {"language": "spa", "title": "Español"}},
                {"codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
            ],
            "video": {"codec_name": "h264", "width": 1920, "height": 1080},
            "direct_source": "https://private.invalid/private-user/private-password/movie.mp4",
        },
        "movie_data": {"stream_id": 10, "container_extension": "mkv"},
    }


def ice_age_info():
    return {
        "info": {
            "name": "Ice Age 4: Continental Drift", "genre": "Animation, Comedy, Adventure",
            "cast": "Ray Romano, John Leguizamo", "director": "Michael Thurmeier, Steve Martino",
            "release_date": "2012-06-27", "rating": 6.39, "plot": "Sinopsis de la obra",
            "duration": "01:27:45", "duration_secs": 5265, "bitrate": 2379,
            "subtitles": [{"index": index, "language": language, "title": None}
                          for index, language in enumerate(["spa"] * 4 + ["eng"] * 2)],
        },
        "movie_data": {"container_extension": "mp4", "title": "Ice Age 4: Continental Drift"},
    }


class ProviderClientTests(unittest.TestCase):
    def setUp(self):
        self.client = XtreamClient("https://provider.invalid", "private-user", "private-password")
        self.addCleanup(self.client.close)

    def response(self, payload, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.json.return_value = payload
        return response

    def test_details_use_correct_actions_ids_and_do_not_change_catalog_fetch(self):
        for kind, action, key, payload in (
            ("movies", "get_vod_info", "vod_id", movie_info()),
            ("series", "get_series_info", "series_id", {"info": {"name": "Serie"}, "episodes": {}}),
        ):
            with self.subTest(kind=kind), patch.object(
                self.client.info_session, "get", return_value=self.response(payload)
            ) as get:
                self.assertEqual(self.client.fetch_info(kind, "545621"), payload)
                kwargs = get.call_args.kwargs
                self.assertEqual(kwargs["params"], {
                    "username": "private-user", "password": "private-password",
                    "action": action, key: "545621",
                })
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(kwargs["timeout"], (10, 15))
                self.assertEqual(self.client.info_session.get_adapter("https://").max_retries.total, 0)
        with patch.object(self.client.session, "get", return_value=self.response([{"name": "Movie"}])):
            self.assertEqual(self.client.fetch("get_vod_streams"), [{"name": "Movie"}])

    def test_invalid_input_and_responses_are_explicit_errors(self):
        with patch.object(self.client.info_session, "get") as get:
            for kind, identifier in (("live", "10"), ("movies", "10&password=bad"), ("movies", "١٠"), ("movies", 10)):
                with self.assertRaises(CatalogError):
                    self.client.fetch_info(kind, identifier)
            get.assert_not_called()
        for payload in (None, [], {}, {"user_info": {"auth": 0}}, {"info": []}, {"info": {"name": "x"}, "movie_data": []}):
            with self.subTest(payload=payload), patch.object(
                self.client.info_session, "get", return_value=self.response(payload)
            ), self.assertRaises(CatalogError):
                self.client.fetch_info("movies", "10")
        for episodes in ({"1": {}}, {"1": [None]}, "invalid"):
            with self.subTest(episodes=episodes), patch.object(
                self.client.info_session, "get", return_value=self.response({"info": {"name": "Serie"}, "episodes": episodes})
            ), self.assertRaises(CatalogError):
                self.client.fetch_info("series", "20")

    def test_transport_errors_never_expose_credentials(self):
        cases = [
            {"return_value": self.response(None, 503)},
            {"side_effect": requests.Timeout("private-password")},
            {"side_effect": requests.ConnectionError("https://private-user:private-password@provider.invalid")},
            {"side_effect": requests.exceptions.SSLError("private-password")},
        ]
        malformed = self.response(None)
        malformed.json.side_effect = ValueError("private-password")
        cases.append({"return_value": malformed})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), patch.object(self.client.info_session, "get", **kwargs):
                with self.assertRaises(CatalogError) as error:
                    self.client.fetch_info("movies", "10")
                self.assertNotIn("private-password", str(error.exception))
                self.assertNotIn("private-user", str(error.exception))


class ProviderNormalizationTests(unittest.TestCase):
    def test_only_copy_specific_fields_and_one_duration_are_retained(self):
        data = normalize_provider_info(ice_age_info(), "movies")
        self.assertEqual(data["fields"], [
            {"label": "Duración del archivo", "value": "01:27:45"},
            {"label": "Tasa de bits", "value": "2379"},
            {"label": "Formato", "value": "mp4"},
        ])
        self.assertEqual(data["audio"], [])
        raw = ice_age_info()
        del raw["info"]["duration"]
        self.assertIn({"label": "Duración (segundos)", "value": "5265"},
                      normalize_provider_info(raw, "movies")["fields"])

    def test_legacy_cached_fields_are_filtered_without_mutating_cache(self):
        data = {"fields": [
            {"label": "Nombre", "value": "Duplicado"},
            {"label": "Duración", "value": "01:27:45"},
            {"label": "Duración (segundos)", "value": "5265"},
            {"label": "Tasa de bits declarada", "value": "2379"},
            {"label": "Formato", "value": "mp4"},
        ], "audio": [], "video": [], "subtitles": [], "episodes": []}
        original = copy.deepcopy(data)
        view = provider_presentation(data, "movies")
        self.assertEqual(data, original)
        self.assertEqual([field["label"] for field in view["fields"]],
                         ["Duración del archivo", "Tasa de bits", "Formato"])

    def test_real_provider_subtitles_do_not_imply_audio_languages(self):
        data = normalize_provider_info({
            "info": {
                "name": "Ice Age 4: Continental Drift",
                "subtitles": [
                    {"index": index, "language": language, "title": None}
                    for index, language in enumerate(["spa"] * 4 + ["eng"] * 2)
                ],
            },
            "movie_data": {"container_extension": "mp4"},
        }, "movies")
        self.assertEqual(data["audio"], [])
        self.assertEqual(len(data["subtitles"]), 6)
        self.assertEqual([track[0]["value"] for track in data["subtitles"]],
                         ["Español (spa)"] * 4 + ["Inglés (eng)"] * 2)
        self.assertEqual([track[1]["value"] for track in data["subtitles"]],
                         [str(index) for index in range(6)])

    def test_diagnostic_keeps_unknown_fields_but_redacts_nested_private_data(self):
        payload = {
            "unknown": {"array": [True, None, 42, {"audio_language": "spa"}]},
            "password": "unrecognized-secret", "nested": {
                "access_token": "unrecognized-token", "username": "someone",
                "direct_source": "/movie/private-user/private-password/10.mkv",
                "url": "HTTPS://example.invalid/private-user/private-password",
                "description": "private-user private-password p%2Fa%20ss p%2Fa+ss",
            },
            "long": "x" * 5000,
        }
        diagnostic = provider_diagnostic(payload, ("private-user", "private-password", "p/a ss"))
        serialized = json.dumps(diagnostic)
        for secret in ("unrecognized-secret", "unrecognized-token", "someone",
                       "private-user", "private-password", "example.invalid", "p%2Fa"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(diagnostic["unknown"], payload["unknown"])
        self.assertEqual(diagnostic["long"], payload["long"])
        self.assertEqual(set(diagnostic), set(payload))
        self.assertEqual(payload["password"], "unrecognized-secret")

    def test_only_safe_fields_are_retained_without_claiming_unlabelled_audio(self):
        payload = movie_info()
        payload["info"]["plot"] = "Source https://private.invalid/private-user/private-password and private-password"
        payload["info"]["video"]["extradata"] = "private-password"
        normalized = normalize_provider_info(payload, "movies", ("private-user", "private-password"))
        serialized = json.dumps(normalized)
        for secret in ("https://", "private-user", "private-password", "direct_source", "extradata"):
            self.assertNotIn(secret, serialized)
        self.assertIn("spa", serialized)
        self.assertIn("eng", serialized)
        self.assertIn("1920", serialized)
        self.assertIn("mkv", serialized)
        payload["info"]["audio"] = {"codec_name": "aac", "channels": 2}
        normalized = normalize_provider_info(payload, "movies")
        self.assertEqual(normalized["audio"][0][0]["value"], "Idioma no informado")
        self.assertNotIn("eng", json.dumps(normalized))

    def test_series_audio_is_per_episode_and_never_inferred_from_title(self):
        normalized = normalize_provider_info({
            "info": {"name": "Serie español", "language": "en"},
            "episodes": {
                "2": [
                    {"episode_num": 1, "title": "Primero", "container_extension": "mp4",
                     "info": {"audio": {"tags": {"language": "spa"}}}},
                    {"episode_num": 2, "title": "Segundo", "info": {"audio": {"codec_name": "aac"}}},
                ],
            },
        }, "series")
        self.assertEqual(normalized["audio"], [])
        self.assertEqual([ep["season"] for ep in normalized["episodes"]], ["2", "2"])
        self.assertEqual(normalized["episodes"][0]["audio"][0][0]["value"], "Español (spa)")
        self.assertEqual(normalized["episodes"][1]["audio"][0][0]["value"], "Idioma no informado")
        self.assertIn("mp4", json.dumps(normalized["episodes"][0]))

    def test_streams_and_special_seasons_preserve_declared_values(self):
        data = normalize_provider_info({
            "info": {"streams": [
                {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "und"}},
                {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "es-MX"}},
                {"codec_type": "video", "codec_name": "hevc", "height": 2160},
            ]},
            "seasons": [{"season_number": 0, "name": "Especiales", "episode_count": 1}],
            "episodes": [],
        }, "series")
        self.assertEqual(data["audio"][0][0]["value"], "Idioma no informado")
        self.assertEqual(data["subtitles"][0][0]["value"], "Español (es-MX)")
        self.assertEqual(data["seasons"][0]["number"], "0")
        self.assertIn("2160", json.dumps(data["video"]))


class ProviderInfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.store.replace(self.data)
        self.provider = Mock()
        self.provider.credentials = {"username": "private-user", "password": "private-password"}
        self.provider.fetch_info.return_value = movie_info()
        self.factory = Mock(return_value=self.provider)
        self.app.config["XTREAM_CLIENT_FACTORY"] = self.factory
        self.client = self.app.test_client()
        self.client.get("/catalog/movies/detail?id=10")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def post(self, kind="movies", record_id="10", **data):
        return self.client.post(f"/catalog/{kind}/{record_id}/provider-info?children=all&watched=all",
                                data={"csrf": self.csrf, **data}, follow_redirects=True)

    def test_manual_query_caches_only_this_copy_and_get_never_contacts_provider(self):
        initial = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn("Consultar proveedor", initial)
        self.assertNotIn("open", re.search(r'<details id="provider-info"[^>]*>', initial).group())
        self.assertGreater(initial.index('id="provider-info"'), initial.index('id="tmdb-section"'))
        self.factory.assert_not_called()
        page = self.post()
        self.assertEqual(page.status_code, 200)
        self.assertIn("guardada para esta copia", page.get_data(as_text=True))
        self.assertIn("spa", page.get_data(as_text=True))
        self.assertIn("eng", page.get_data(as_text=True))
        self.provider.fetch_info.assert_called_once_with("movies", "10")
        self.provider.close.assert_called_once()
        cached = CatalogStore(self.path).provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))
        self.assertFalse(cached["stale"])
        self.assertIsNotNone(cached["checked_at"])
        self.assertIsNone(self.store.provider_cached("movies", "11", ""))
        self.client.get("/catalog/movies/detail?id=10")
        self.provider.fetch_info.assert_called_once()
        self.assertNotIn("direct_source", json.dumps(cached))

    def test_existing_diagnostic_is_reinterpreted_without_provider_request(self):
        old = {"fields": [{"label": "Reparto", "value": "Ray Romano"}],
               "audio": [], "subtitles": [], "video": [], "episodes": [], "diagnostic": ice_age_info()}
        self.store.save_provider_info("movies", "10", provider_fingerprint(self.data["movies"][0]), old)
        html = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        panel = html.split('<details id="provider-info"', 1)[1]
        main_panel = panel.split('JSON de diagnóstico', 1)[0]
        for label in ("Reparto", "Dirección", "Estreno", "Valoración del proveedor", "Duración (segundos)", "Género"):
            self.assertNotIn(label, main_panel)
        self.assertIn("Español (spa) · 4 pistas", main_panel)
        self.assertIn("Inglés (eng) · 2 pistas", main_panel)
        self.assertIn("Duración del archivo", main_panel)
        self.assertIn("01:27:45", main_panel)
        self.assertIn("No permite confirmar ni descartar audio en español", main_panel)
        self.assertEqual(main_panel.count("Índice de pista"), 6)
        self.assertIn("Detalles de pistas de subtítulos (6)", main_panel)
        self.assertIn('id="provider-diagnostic-json"', panel)
        self.factory.assert_not_called()
        self.assertEqual(self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))["data"], old)

    def test_optional_diagnostic_is_persistent_safe_and_removed_on_normal_refresh(self):
        self.provider.fetch_info.return_value["extra"] = {
            "unmapped_audio": ["spa", "eng"], "password": "other-secret",
            "title": "</pre><script>alert(1)</script>",
        }
        page = self.post(provider_diagnostic="1")
        html = page.get_data(as_text=True)
        self.assertIn('id="provider-diagnostic-json"', html)
        self.assertIn("unmapped_audio", html)
        self.assertNotIn("</pre><script>", html)
        self.assertNotIn("other-secret", html)
        self.assertNotIn("private-password", html)
        cached = CatalogStore(self.path).provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))
        self.assertEqual(cached["data"]["diagnostic"]["extra"]["unmapped_audio"], ["spa", "eng"])
        self.assertEqual(cached["data"]["diagnostic"]["info"]["direct_source"], "[dato privado]")
        self.provider.fetch_info.side_effect = CatalogError("HTTP 503")
        with self.assertLogs(self.app.logger, level="WARNING"):
            html = self.post().get_data(as_text=True)
        self.assertIn('id="provider-diagnostic-json"', html)
        self.provider.fetch_info.side_effect = None
        html = self.post().get_data(as_text=True)
        self.assertNotIn('id="provider-diagnostic-json"', html)
        cached = self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))
        self.assertNotIn("diagnostic", cached["data"])

    def test_failures_preserve_previous_cache_and_show_warning(self):
        self.post()
        cached = self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))
        self.provider.fetch_info.side_effect = CatalogError("get_vod_info: la API respondió HTTP 503.")
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            page = self.post()
        self.assertIn("HTTP 503", page.get_data(as_text=True))
        self.assertIn("No se sustituyeron los datos guardados", page.get_data(as_text=True))
        self.assertIn("spa", page.get_data(as_text=True))
        self.assertIn("HTTP 503", " ".join(logs.output))
        self.assertEqual(cached, self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0])))
        self.assertEqual(self.provider.close.call_count, 2)

    def test_storage_and_configuration_errors_are_visible_without_private_details(self):
        self.post()
        cached = self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0]))
        with patch.object(self.store, "save_provider_info", side_effect=sqlite3.OperationalError("private-password")):
            with self.assertLogs(self.app.logger, level="WARNING") as logs:
                page = self.post()
            self.assertIn("No se pudo guardar la ficha", page.get_data(as_text=True))
            self.assertNotIn("private-password", page.get_data(as_text=True) + " ".join(logs.output))
        self.assertEqual(cached, self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0])))
        self.factory.side_effect = CatalogError("Completa la configuración de Xtream.")
        with self.assertLogs(self.app.logger, level="WARNING"):
            page = self.post()
        self.assertIn("Completa la configuración", page.get_data(as_text=True))
        self.assertEqual(cached, self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0])))

    def test_csrf_validation_and_missing_titles_do_not_contact_provider(self):
        for token in ("", "bad", "á"):
            response = self.client.post("/catalog/movies/10/provider-info", data={"csrf": token})
            self.assertEqual(response.status_code, 400)
        for kind, identifier, status in (("movies", "999", 404), ("channels", "10", 404)):
            self.assertEqual(self.client.post(f"/catalog/{kind}/{identifier}/provider-info",
                                             data={"csrf": self.csrf}).status_code, status)
        self.assertEqual(self.client.post("/catalog/movies/10/provider-info?sort_by=invalid",
                                         data={"csrf": self.csrf}).status_code, 400)
        self.assertEqual(self.client.post("/catalog/movies/10/provider-info",
                                         data={"csrf": self.csrf, "provider_record_id": "11"}).status_code, 400)
        self.assertEqual(self.client.post("/catalog/movies/10/provider-info",
                                         data={"csrf": self.csrf, "provider_diagnostic": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get("/catalog/movies/10/provider-info").status_code, 405)
        self.factory.assert_not_called()

    def test_series_empty_audio_html_escaping_and_episodes(self):
        self.provider.fetch_info.return_value = {
            "info": {"name": "<script>unsafe</script>"},
            "episodes": {"1": [{"title": "<img src=x onerror=alert(1)>", "episode_num": 1,
                                "info": {"audio": {"codec_name": "aac"}}}]},
        }
        page = self.post("series", "20").get_data(as_text=True)
        self.provider.fetch_info.assert_called_once_with("series", "20")
        self.assertIn("Idioma no informado", page)
        self.assertIn("Copias de episodios (1)", page)
        self.assertNotIn("unsafe", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("aac", page)

    def test_cache_survives_sync_flags_changed_catalog_and_cleans_deleted_title(self):
        self.post()
        self.store.replace(self.data)
        self.assertIsNotNone(self.store.provider_cached("movies", "10", provider_fingerprint(self.data["movies"][0])))
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["container_extension"] = "mkv"
        self.store.replace(changed)
        self.assertTrue(self.store.provider_cached("movies", "10", provider_fingerprint(changed["movies"][0]))["stale"])
        page = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn("cambió desde la última consulta", page)
        changed["movies"] = [row for row in changed["movies"] if row["stream_id"] != 10]
        self.store.replace(changed)
        self.assertIsNone(self.store.provider_cached("movies", "10", ""))

    def test_grouped_versions_keep_independent_results_and_validate_selection(self):
        for record_id in ("10", "11"):
            raw = self.store.detail("movies", record_id)["data"]
            self.store.save_tmdb("movies", record_id, "CA", fingerprint(raw),
                                 {"status": "matched", "id": 123, "title": "Same film"})
        page = self.post(provider_record_id="11")
        self.assertEqual(page.status_code, 200)
        self.provider.fetch_info.assert_called_once_with("movies", "11")
        self.assertIn('id="provider-record-id"', page.get_data(as_text=True))
        self.assertIsNone(self.store.provider_cached("movies", "10", ""))
        self.assertIsNotNone(self.store.provider_cached("movies", "11", provider_fingerprint(self.data["movies"][1])))
        self.assertIn("ID 11", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
