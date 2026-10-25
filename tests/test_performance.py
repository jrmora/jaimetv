import re
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from app import create_app
from catalog import PERFORMANCE_TIMINGS
from test_app import snapshot
from tmdb import TMDBClient, TMDBError


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.config["CATALOG_STORE"].replace(snapshot())
        self.client = self.app.test_client()

    def test_request_timings_and_logs_exclude_search_values(self):
        with self.assertLogs(self.app.logger, level="INFO") as captured:
            response = self.client.get("/api/catalog/movies?view=cards&q=private-search-text")
        self.assertEqual(response.status_code, 200)
        timings = response.headers["Server-Timing"]
        for phase in ("refresh_index", "prepare_versions", "rating_mean", "count", "select_rows", "commit", "sqlite", "total"):
            self.assertRegex(timings, rf"(?:^|, ){phase};dur=\d+\.\d")
        request_id = response.headers["X-Request-ID"]
        self.assertRegex(request_id, r"^[0-9a-f]{16}$")
        logs = "\n".join(captured.output)
        self.assertIn(f"request_id={request_id}", logs)
        self.assertIn("endpoint=catalog_api", logs)
        self.assertNotIn("private-search-text", logs)
        self.assertIsNone(PERFORMANCE_TIMINGS.get())

    def test_timings_do_not_leak_to_next_request_or_error(self):
        first = self.client.get("/api/catalog/movies?view=cards")
        second = self.client.get("/movies?ordering=invalid")
        self.assertEqual(second.status_code, 400)
        self.assertNotEqual(first.headers["X-Request-ID"], second.headers["X-Request-ID"])
        self.assertNotIn("select_rows", second.headers["Server-Timing"])
        self.assertIsNone(PERFORMANCE_TIMINGS.get())

    def test_sum_of_query_phases_does_not_exceed_sqlite_duration(self):
        response = self.client.get("/api/catalog/movies?view=cards")
        timings = {name: float(value) for name, value in re.findall(r"(\w+);dur=([\d.]+)", response.headers["Server-Timing"])}
        self.assertLessEqual(sum(timings[name] for name in ("refresh_index", "prepare_versions", "rating_mean", "count", "select_rows")), timings["sqlite"] + 0.4)

    def test_tmdb_wait_and_http_timings_include_failed_requests(self):
        config = Path(self.temp.name) / "tmdb.json"
        config.write_text('{"api_key": "test"}')
        client = TMDBClient(config)
        timings = {}
        token = PERFORMANCE_TIMINGS.set(timings)
        try:
            with patch("tmdb.REQUEST_GATE.request", side_effect=nullcontext), \
                    patch.object(client, "_get", side_effect=TMDBError("Test failure")):
                with self.assertRaises(TMDBError):
                    client.get("movie/1")
            self.assertIn("tmdb_wait", timings)
            self.assertIn("tmdb_http", timings)
        finally:
            PERFORMANCE_TIMINGS.reset(token)

    def test_failed_database_transaction_records_rollback_time(self):
        timings = {}
        token = PERFORMANCE_TIMINGS.set(timings)
        try:
            with self.assertRaisesRegex(RuntimeError, "Test failure"):
                with self.app.config["CATALOG_STORE"].connect() as db:
                    db.execute("UPDATE movies SET name='Rollback test'")
                    raise RuntimeError("Test failure")
            self.assertIn("commit", timings)
            self.assertIn("sqlite", timings)
        finally:
            PERFORMANCE_TIMINGS.reset(token)
