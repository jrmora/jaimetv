import copy
import json
import logging
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from app import create_app
from catalog import CatalogStore, fingerprint
from movie_audio import MovieAudioJobs, audio_tracks, copy_fingerprint, movie_url, probe_movie
from test_app import snapshot
from xtream import CatalogError, XtreamClient


class AudioTracksTests(unittest.TestCase):
    def test_only_audio_languages_not_subtitles_or_original_language(self):
        self.assertEqual(audio_tracks({"streams": [
            {"codec_type": "audio", "tags": {"language": "spa"}},
            {"codec_type": "audio", "tags": {"language": "es-419"}},
            {"codec_type": "audio", "tags": {"language": "eng"}},
            {"codec_type": "audio", "tags": {"language": "EN_us"}},
            {"codec_type": "audio", "tags": {"language": "fre"}},
            {"codec_type": "subtitle", "tags": {"language": "por"}},
            {"codec_type": "video", "tags": {"language": "jpn"}},
            *[{"codec_type": "audio", "tags": {"language": value}}
              for value in ("und", "", None, "<script>", "mul")],
        ]}), {"languages": ["ES", "EN", "FR"], "unknown": 5, "tracks": 10})

    def test_empty_and_malformed_results(self):
        self.assertEqual(audio_tracks({"streams": []}), {"languages": [], "unknown": 0, "tracks": 0})
        for value in (None, {}, {"streams": None}, {"streams": ["audio"]}, {"streams": [{}]}):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                audio_tracks(value)

    def test_url_is_constructed_not_taken_from_provider_json(self):
        client = XtreamClient("https://example.invalid/path", "user/name", "secret/pass")
        self.addCleanup(client.close)
        url = movie_url(client, "10", {"container_extension": "mp4", "direct_source": "http://127.0.0.1/private"})
        self.assertEqual(url, "https://example.invalid/path/movie/user%2Fname/secret%2Fpass/10.mp4")
        for record_id, extension in (("../10", "mp4"), ("10", "../secret"), ("10", None)):
            with self.subTest(record_id=record_id, extension=extension), self.assertRaises(CatalogError):
                movie_url(client, record_id, {"container_extension": extension})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.paths = []
        self.ranges = []
        self.status = 206
        self.body = b"media header"

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                owner.paths.append(self.path)
                owner.ranges.append(self.headers.get("Range"))
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(owner.body)))
                if owner.status == 206:
                    self.send_header("Content-Range", f"bytes 0-{len(owner.body) - 1}/{len(owner.body)}")
                if owner.status == 302:
                    self.send_header("Location", "http://127.0.0.1/private")
                self.end_headers()
                try:
                    self.wfile.write(owner.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = XtreamClient(f"http://127.0.0.1:{self.server.server_port}", "user", "private-password")
        self.addCleanup(self.client.close)

    def fake_ffprobe(self, command, **kwargs):
        self.assertNotIn("private-password", " ".join(command))
        self.assertEqual(urlsplit(command[-1]).hostname, "127.0.0.1")
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        with requests.get(command[-1], headers={"Range": "bytes=0-"}, timeout=2) as response:
            response.content
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "streams": [{"codec_type": "audio", "tags": {"language": "spa"}}],
        }).encode())

    def test_bridge_forwards_range_and_parses_audio_without_exposing_credentials(self):
        with patch("movie_audio.shutil.which", return_value="/usr/bin/ffprobe"), patch(
            "movie_audio.subprocess.run", side_effect=self.fake_ffprobe,
        ):
            result = probe_movie(self.client, "10", {"container_extension": "mp4"})
        self.assertEqual(result, {"languages": ["ES"], "unknown": 0, "tracks": 1})
        self.assertEqual(self.ranges, ["bytes=0-"])
        self.assertEqual(len(self.paths), 1)

    def test_byte_budget_blocks_even_a_success_shaped_probe_result(self):
        def fake(command, **kwargs):
            try:
                requests.get(command[-1], timeout=2)
            except requests.RequestException:
                pass
            return subprocess.CompletedProcess(command, 0, b'{"streams":[]}')
        self.body = b"x" * 100
        with patch("movie_audio.shutil.which", return_value="/ffprobe"), patch(
            "movie_audio.MAX_BYTES", 8,
        ), patch("movie_audio.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(CatalogError, "límite"):
                probe_movie(self.client, "10", {"container_extension": "mp4"})

    def test_redirect_is_not_followed(self):
        self.status = 302
        with patch("movie_audio.shutil.which", return_value="/ffprobe"), patch(
            "movie_audio.subprocess.run", side_effect=self.fake_ffprobe,
        ):
            with self.assertRaisesRegex(CatalogError, "redirige"):
                probe_movie(self.client, "10", {"container_extension": "mp4"})
        self.assertEqual(len(self.paths), 1)

    def test_request_budget_is_explicit(self):
        def excessive_requests(command, **kwargs):
            for _ in range(17):
                requests.get(command[-1], timeout=2).close()
            return subprocess.CompletedProcess(command, 0, b'{"streams":[]}')
        self.body = b""
        with patch("movie_audio.shutil.which", return_value="/ffprobe"), patch(
            "movie_audio.subprocess.run", side_effect=excessive_requests,
        ):
            with self.assertRaisesRegex(CatalogError, "límite"):
                probe_movie(self.client, "10", {"container_extension": "mp4"})
        self.assertEqual(len(self.paths), 16)

    def test_missing_tool_timeout_invalid_output_and_failed_process_are_explicit(self):
        with patch("movie_audio.shutil.which", return_value=None):
            with self.assertRaisesRegex(CatalogError, "Falta ffprobe"):
                probe_movie(self.client, "10", {"container_extension": "mp4"})
        with patch("movie_audio.shutil.which", return_value="/ffprobe"):
            for result, text in (
                (subprocess.TimeoutExpired("ffprobe", 20), "20 segundos"),
                (subprocess.CompletedProcess([], 1, b""), "identificar"),
                (subprocess.CompletedProcess([], 0, b"not-json"), "válidos"),
                (subprocess.CompletedProcess([], 0, b"{}"), "pistas válidas"),
            ):
                with self.subTest(text=text):
                    options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                    with patch("movie_audio.subprocess.run", **options), self.assertRaisesRegex(CatalogError, text):
                        probe_movie(self.client, "10", {"container_extension": "mp4"})
        self.assertEqual(self.paths, [])


class AudioWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.app = create_app(self.path)
        self.app.logger.setLevel(logging.CRITICAL)
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.data["movies"][1]["container_extension"] = "mkv"
        self.store.replace(self.data)
        self.client = self.app.test_client()
        self.client.get("/catalog/movies/detail?id=10")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]
        self.factory = Mock(return_value=Mock())
        self.probe = Mock(return_value={"languages": ["ES", "EN"], "unknown": 1, "tracks": 3})
        self.app.config.update(XTREAM_CLIENT_FACTORY=self.factory, MOVIE_AUDIO_PROBE=self.probe)
        self.jobs = self.app.config["MOVIE_AUDIO_JOBS"]

    def wait(self, record_id="10"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.jobs.status(record_id)
            if not state["running"]:
                return state
            time.sleep(0.01)
        self.fail("Audio worker did not finish")

    def start(self, record_id="10"):
        return self.client.post(f"/api/movies/{record_id}/audio", data={"csrf": self.csrf})

    def test_manual_only_top_of_detail_cache_restart_and_copy_isolation(self):
        html = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertLess(html.index('id="movie-audio"'), html.index('id="content-labels"'))
        self.client.get("/movies")
        self.client.get("/api/movies/10/audio")
        self.factory.assert_not_called()
        self.probe.assert_not_called()
        self.assertEqual(self.start().status_code, 202)
        state = self.wait()
        self.assertEqual(state["metadata"], self.probe.return_value)
        self.assertIsNotNone(state["checked_at"])
        self.assertIsNone(self.jobs.status("11")["metadata"])
        reopened = MovieAudioJobs(CatalogStore(self.path), self.app.logger)
        self.assertEqual(reopened.status("10"), state)
        self.store.sync(Mock(download=Mock(return_value=self.data)))
        self.assertEqual(self.jobs.status("10")["metadata"], self.probe.return_value)
        html = self.client.get("/catalog/movies/detail?id=10").get_data(as_text=True)
        self.assertIn(">ES</span>", html)
        self.assertIn(">EN</span>", html)
        self.assertIn(">?</span>", html)
        self.assertEqual(self.probe.call_count, 1)
        self.factory.return_value.close.assert_called_once()

    def test_csrf_missing_title_series_route_and_no_store(self):
        self.assertEqual(self.client.post("/api/movies/10/audio").status_code, 400)
        self.assertEqual(self.client.post("/api/movies/999/audio", data={"csrf": self.csrf}).status_code, 404)
        self.assertEqual(self.client.post("/api/series/20/audio", data={"csrf": self.csrf}).status_code, 404)
        self.assertNotIn('id="movie-audio"', self.client.get("/catalog/series/detail?id=20").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/movies/10/audio").headers["Cache-Control"], "no-store")
        self.probe.assert_not_called()

    def test_error_preserves_cache_and_changed_copy_invalidates_it(self):
        self.assertEqual(self.start().status_code, 202)
        self.wait()
        self.probe.side_effect = CatalogError("No se pudo leer el vídeo.")
        self.assertEqual(self.start().status_code, 202)
        state = self.wait()
        self.assertEqual(state["metadata"], self.probe.return_value)
        self.assertIn("No se pudo", state["error"])
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["container_extension"] = "mkv"
        self.store.replace(changed)
        state = self.jobs.status("10")
        self.assertIsNone(state["metadata"])
        self.assertTrue(state["stale"])
        self.assertIn("cambió", state["error"])

    def test_error_is_not_lost_when_a_different_copy_is_checked_next(self):
        self.probe.side_effect = CatalogError("No se pudo comprobar esta copia.")
        self.assertEqual(self.start().status_code, 202)
        error = self.wait()["error"]
        self.probe.side_effect = None
        self.assertEqual(self.start("11").status_code, 202)
        self.wait("11")
        self.assertEqual(self.jobs.status("10")["error"], error)

    def test_one_job_across_app_instances_and_fast_start_without_blocking_navigation(self):
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)

        def slow(*args):
            entered.set()
            release.wait(2)
            return self.probe.return_value

        self.app.config["MOVIE_AUDIO_PROBE"] = slow
        self.assertEqual(self.start().status_code, 202)
        self.assertTrue(entered.wait(1))
        try:
            other = MovieAudioJobs(CatalogStore(self.path), self.app.logger)
            self.assertFalse(other.start("11", self.factory, self.probe))
            self.assertEqual(self.start("11").status_code, 409)
            self.assertEqual(self.client.get("/movies").status_code, 200)
            self.assertTrue(self.client.get("/api/movies/10/audio").json["running"])
        finally:
            release.set()
            self.wait()

    def test_changed_during_probe_does_not_save_and_stale_jobs_can_restart(self):
        def changing(*args):
            changed = copy.deepcopy(self.data)
            changed["movies"][0]["name"] = "Different title"
            self.store.replace(changed)
            return self.probe.return_value
        self.app.config["MOVIE_AUDIO_PROBE"] = changing
        self.assertEqual(self.start().status_code, 202)
        self.assertIn("cambió", self.wait()["error"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movie_audio_metadata").fetchone()[0], 0)
            raw = self.store.detail("movies", "10")["data"]
            db.execute("UPDATE movie_audio_job SET status='running', started_at=?, fingerprint=?",
                       (time.time() - 90, copy_fingerprint(raw)))
        self.assertIn("interrumpió", self.jobs.status("10")["error"] or "")
        self.app.config["MOVIE_AUDIO_PROBE"] = self.probe
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.wait()["metadata"], self.probe.return_value)

    def test_grouped_versions_keep_independent_audio(self):
        for record_id in ("10", "11"):
            raw = self.store.detail("movies", record_id)["data"]
            self.store.save_tmdb("movies", record_id, "CA", fingerprint(raw),
                                 {"status": "matched", "id": 123, "title": "Batman"})
        self.assertEqual(self.start().status_code, 202)
        self.wait()
        self.assertEqual(self.store.detail("movies", "11")["version_count"], 2)
        html = self.client.get("/catalog/movies/detail?id=11").get_data(as_text=True)
        self.assertIn('id="audio-copy"', html)
        self.assertIn('data-audio-copy="10"', html)
        self.assertIn('data-audio-copy="11"', html)
        self.assertIsNone(self.jobs.status("11")["metadata"])

    def test_unavailable_copy_is_not_probed_and_removed_copy_cache_is_cleaned(self):
        self.assertEqual(self.start().status_code, 202)
        self.wait()
        with self.store.connect() as db:
            db.execute("INSERT INTO provider_removed (kind, record_id, removed_at) VALUES ('movies', '10', 'now')")
        self.assertEqual(self.start().status_code, 400)
        self.assertEqual(self.probe.call_count, 1)
        changed = copy.deepcopy(self.data)
        changed["movies"] = [row for row in changed["movies"] if row["stream_id"] != 10]
        self.store.replace(changed)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movie_audio_metadata").fetchone()[0], 0)
