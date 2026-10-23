import json
import io
import os
import sqlite3
import tarfile
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import create_app
from updater import deploy, read_state, write_state


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        auth = self.root / "auth.json"
        auth.write_text(json.dumps({
            "username": "jaime", "password_hash": generate_password_hash("password"),
            "secret_key": "x" * 64,
        }))
        self.state = self.root / "state"
        environment = patch.dict("os.environ", {
            "IPTV_AUTH_PATH": str(auth), "IPTV_UPDATE_STATE": str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.app = create_app(self.root / "catalog.db")
        self.client = self.app.test_client()
        self.client.get("/login", base_url="https://localhost")
        with self.client.session_transaction() as session:
            csrf = session["sync_csrf"]
        self.client.post("/login", base_url="https://localhost", data={
            "username": "jaime", "password": "password", "csrf": csrf,
        })
        self.client.get("/movies", base_url="https://localhost")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def post(self):
        return self.client.post("/api/application-update", base_url="https://localhost",
                                data={"csrf": self.csrf})

    def test_state_survives_process_restart(self):
        write_state(self.state, status="completed", message="done")
        self.assertEqual(read_state(self.state)["status"], "completed")

    def test_requires_authentication_and_csrf(self):
        self.assertEqual(self.app.test_client().post("/api/application-update").status_code, 401)
        self.assertEqual(self.client.post("/api/application-update",
                                         base_url="https://localhost").status_code, 400)

    def test_starts_once_without_confirmation(self):
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().status_code, 409)
        self.assertTrue((self.state / "request").exists())
        self.assertEqual(self.client.post("/api/sync", base_url="https://localhost",
                                          data={"csrf": self.csrf}).status_code, 503)

    def test_launch_failure_is_explicit(self):
        original = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == "request":
                raise OSError
            return original(path, *args, **kwargs)

        with patch.object(Path, "write_text", write):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(read_state(self.state)["status"], "failed")

    def test_batch_prevents_deploy(self):
        self.app.config["TMDB_BATCH"].state["status"] = "running"
        self.assertEqual(self.post().status_code, 409)


class DeploymentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "app"
        self.target.mkdir()
        self.state = self.root / "state"
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.target / "app.py").write_text("old code")
        (self.target / "requirements.txt").write_text("old dependencies")
        (self.target / "services.json").write_text("personal subscriptions")
        (self.target / "auth.local.json").write_text("private")
        with closing(sqlite3.connect(self.target / "catalog.db")) as connection, connection:
            connection.execute("CREATE TABLE data (value TEXT)")
            connection.execute("INSERT INTO data VALUES ('personal')")
        environment = patch.dict(os.environ, {
            "IPTV_UPDATE_REPO": str(self.repository),
            "IPTV_UPDATE_TARGET": str(self.target),
            "IPTV_UPDATE_STATE": str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.entries = {"app.py": "new code", "requirements.txt": "new dependencies",
                        "services.json": "repository subscriptions", "new.py": "new module"}
        self.dirty = False

    def command(self, *args, **kwargs):
        if args[:2] == ("git", "rev-parse"):
            return "abc123\n"
        if args[:2] == ("git", "status"):
            return " M app.py" if self.dirty else ""
        if args[:2] == ("git", "archive"):
            with tarfile.open(args[4], "w") as archive:
                for name, value in self.entries.items():
                    content = value.encode()
                    entry = tarfile.TarInfo(name)
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
        if args[:2] == ("git", "ls-tree"):
            return "\n".join(self.entries)
        return ""

    def execute(self, health=None):
        with patch("updater.run", side_effect=self.command), \
             patch("updater.subprocess.run") as tests, \
             patch("updater.verify_health", side_effect=health):
            deploy()
            return tests

    def test_success_preserves_private_data_and_backs_up(self):
        self.execute()
        self.assertEqual((self.target / "app.py").read_text(), "new code")
        self.assertEqual((self.target / "services.json").read_text(), "personal subscriptions")
        self.assertEqual((self.target / "auth.local.json").read_text(), "private")
        self.assertEqual(read_state(self.state)["status"], "completed")
        backup = next(self.state.glob("backup-*"))
        with closing(sqlite3.connect(backup / "catalog.db")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM data").fetchone()[0], "personal")
        self.assertEqual((backup / "app.py").read_text(), "old code")

    def test_failed_health_rolls_back_and_reports_error(self):
        with self.assertRaisesRegex(RuntimeError, "restauró"):
            self.execute([RuntimeError("unhealthy"), None])
        self.assertEqual((self.target / "app.py").read_text(), "old code")
        self.assertFalse((self.target / "new.py").exists())
        self.assertEqual(read_state(self.state)["status"], "failed")

    def test_dirty_repository_is_not_overwritten(self):
        self.dirty = True
        with self.assertRaisesRegex(RuntimeError, "cambios locales"):
            self.execute()
        self.assertEqual((self.target / "app.py").read_text(), "old code")

    def test_same_revision_skips_installation(self):
        self.state.mkdir()
        (self.state / "revision").write_text("abc123")
        tests = self.execute()
        tests.assert_not_called()
        self.assertEqual(read_state(self.state)["status"], "completed")

    def test_private_files_in_git_are_rejected(self):
        self.entries["auth.local.json"] = "must not deploy"
        with self.assertRaisesRegex(RuntimeError, "privados"):
            self.execute()
        self.assertEqual((self.target / "auth.local.json").read_text(), "private")
