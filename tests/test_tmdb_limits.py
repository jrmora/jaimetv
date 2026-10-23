import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import create_app
from catalog import fingerprint
from test_app import snapshot
from tmdb import TMDBClient, TMDBError, TMDBRateLimitError, TMDBRequestGate, retry_after_seconds
from tmdb_jobs import TMDBBatch


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class TMDBRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / "tmdb.local.json"
        config.write_text(json.dumps({"read_access_token": "test-private-token"}))
        self.first = TMDBClient(config)
        self.second = TMDBClient(config)
        self.clock = Clock()
        self.gate = TMDBRequestGate(self.clock.monotonic, self.clock.sleep)
        patcher = patch("tmdb.REQUEST_GATE", self.gate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_api_request_is_spaced_across_clients_including_directories(self):
        starts = []

        def get(*args, **kwargs):
            starts.append(self.clock.now)
            return Mock(status_code=200, json=lambda: {"results": []})

        with patch("tmdb.requests.get", side_effect=get):
            for i in range(12):
                client = self.first if i % 2 else self.second
                client.get("watch/providers/movie" if i % 3 else "search/movie")
        self.assertEqual(len(starts), 12)
        self.assertTrue(all(b - a >= 0.25 for a, b in zip(starts, starts[1:])))
        self.assertEqual(starts[-1] - starts[0], 2.75)

    def test_threaded_requests_are_serialized_and_paced(self):
        starts = []
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def get(*args, **kwargs):
            starts.append(self.clock.now)
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release request")
            return Mock(status_code=200, json=lambda: {})

        def fetch(client):
            try:
                client.get("search/movie")
            except Exception as exc:
                errors.append(exc)

        with patch("tmdb.requests.get", side_effect=get):
            first = threading.Thread(target=fetch, args=(self.first,))
            second = threading.Thread(target=fetch, args=(self.second,))
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                second.start()
                self.assertEqual(len(starts), 1)
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(starts, [100, 100.25])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_429_honors_retry_after_blocks_other_clients_without_retry(self):
        with patch("tmdb.requests.get", return_value=Mock(status_code=429, headers={"Retry-After": "120"})) as get:
            with self.assertRaises(TMDBRateLimitError) as error:
                self.first.get("search/movie")
            self.assertEqual(error.exception.retry_after, 120)
            self.assertNotIn("test-private-token", str(error.exception))
            with self.assertRaises(TMDBRateLimitError):
                self.second.get("watch/providers/tv")
            get.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])
        self.clock.now += 120
        with patch("tmdb.requests.get", return_value=Mock(status_code=200, json=lambda: {})) as get:
            self.assertEqual(self.second.get("search/tv"), {})
            get.assert_called_once()

    def test_retry_after_http_date_and_invalid_header_fallback(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        with patch("tmdb.datetime") as date:
            date.now.return_value = now
            self.assertEqual(retry_after_seconds(format_datetime(now + timedelta(seconds=95), usegmt=True)), 95)
            self.assertEqual(retry_after_seconds(format_datetime(now - timedelta(seconds=5), usegmt=True)), 1)
        for value in (None, "", "-1", "NaN", "1.5", "private-token", "9" * 1000):
            self.assertEqual(retry_after_seconds(value), 60)
        self.assertEqual(retry_after_seconds("0"), 1)
        self.assertEqual(retry_after_seconds("3600"), 3600)

    def test_429_without_valid_header_uses_shared_sixty_second_cooldown(self):
        with patch("tmdb.requests.get", return_value=Mock(status_code=429, headers={})):
            with self.assertRaises(TMDBRateLimitError) as error:
                self.first.get("search/movie")
        self.assertEqual(error.exception.retry_after, 60)
        self.clock.now += 20
        with patch("tmdb.requests.get") as get:
            with self.assertRaises(TMDBRateLimitError) as error:
                self.second.get("search/movie")
            self.assertEqual(error.exception.retry_after, 40)
            get.assert_not_called()

    def test_other_http_errors_do_not_retry_or_trigger_tls_fallback(self):
        for status in (401, 403, 500, 503):
            with self.subTest(status=status):
                with patch("tmdb.requests.get", return_value=Mock(status_code=status)) as get, patch("tmdb.subprocess.run") as curl:
                    with self.assertRaises(TMDBError):
                        self.first.get("search/movie")
                    get.assert_called_once()
                    curl.assert_not_called()
                    self.assertEqual(self.gate.blocked_until, 0)

    def test_macos_tls_fallback_is_paced_and_reads_retry_after(self):
        starts = []
        temporary_headers = []

        def curl(args, **kwargs):
            starts.append(self.clock.now)
            header_path = Path(args[args.index("--dump-header") + 1])
            temporary_headers.append(header_path)
            header_path.write_text("HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 429\r\nretry-after: 90\r\n\r\n")
            self.assertNotIn("test-private-token", str(args))
            return Mock(returncode=0, stdout='{"status_message":"limited"}\n429')

        with patch("tmdb.requests.get", side_effect=requests.exceptions.SSLError), patch("tmdb.sys.platform", "darwin"), patch("tmdb.subprocess.run", side_effect=curl) as run:
            with self.assertRaises(TMDBRateLimitError) as error:
                self.first.get("search/movie")
            self.assertEqual(error.exception.retry_after, 90)
            self.assertEqual(starts, [100.25])
            self.assertFalse(temporary_headers[0].exists())
            with self.assertRaises(TMDBRateLimitError):
                self.second.get("search/tv")
            run.assert_called_once()

    def test_cached_enrichment_still_available_during_cooldown_and_refresh_returns_429(self):
        app = create_app(Path(self.temp.name) / "catalog.db")
        app.testing = True
        store = app.config["CATALOG_STORE"]
        store.replace(snapshot())
        browser = app.test_client()
        browser.get("/catalog/movies/detail?id=10")
        with browser.session_transaction() as session:
            csrf = session["tmdb_csrf"]
        app.config["TMDB_CLIENT_FACTORY"] = lambda: self.first
        self.gate.blocked_until = self.clock.now + 60
        with patch("tmdb.requests.get") as get:
            response = browser.get("/api/tmdb/movies/10")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "60")
            get.assert_not_called()
        # A valid cached "missing" result also requires no external requests.
        store.save_tmdb("movies", "10", "CA", fingerprint(store.detail("movies", "10")["data"]),
                        {"status": "missing", "checked_at": "2026-10-23", "country": "CA"})
        with patch("tmdb.requests.get") as get:
            self.assertEqual(browser.get("/api/tmdb/movies/10").status_code, 200)
            self.assertEqual(browser.post("/api/tmdb/movies/10", data={"csrf": csrf}).status_code, 429)
            self.assertEqual(browser.get("/api/tmdb/movies/10").status_code, 200)
            get.assert_not_called()

    def test_batch_stops_on_real_client_429_and_cannot_bypass_cooldown(self):
        app = create_app(Path(self.temp.name) / "catalog.db")
        store = app.config["CATALOG_STORE"]
        store.replace(snapshot())
        job = TMDBBatch(store, app.logger)
        # Run the worker deterministically without launching its thread.
        with patch("tmdb_jobs.threading.Thread"):
            self.assertTrue(job.start("movies", 10, lambda: self.first))
        with patch("tmdb.requests.get", return_value=Mock(status_code=429, headers={"Retry-After": "80"})) as get:
            job.run("movies", 10, lambda: self.first)
            self.assertEqual(job.status()["status"], "failed")
            self.assertIn("80 segundos", job.status()["error"])
            self.assertEqual(store.tmdb_coverage("movies")["checked"], 0)
            with patch("tmdb_jobs.threading.Thread"):
                self.assertTrue(job.start("movies", 10, lambda: self.second))
            job.run("movies", 10, lambda: self.second)
            self.assertEqual(job.status()["status"], "failed")
            get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
