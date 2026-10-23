import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import create_app


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "auth.local.json"
        self.config = {
            "username": "jaime",
            "password_hash": generate_password_hash("test-password", method="pbkdf2:sha256:1000"),
            "secret_key": "a" * 64,
        }
        self.path.write_text(json.dumps(self.config))
        self.environment = patch.dict("os.environ", {"IPTV_AUTH_PATH": str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(self.root / "catalog.db")
        self.app.testing = True
        self.client = self.app.test_client()

    def token(self):
        self.client.get("/login", base_url="https://localhost")
        with self.client.session_transaction() as session:
            return session["sync_csrf"]

    def login(self, password="test-password"):
        return self.client.post("/login", base_url="https://localhost", data={
            "username": "jaime", "password": password, "csrf": self.token(),
        })

    def test_all_private_surfaces_require_login(self):
        for path in ("/movies", "/series", "/channels", "/catalog/movies/1", "/admin"):
            self.assertEqual(self.client.get(path).location, "/login")
        for method, path in (("get", "/api/catalog/movies"), ("post", "/api/sync"),
                             ("post", "/catalog/movies/1/watched")):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401)
        with self.client.get("/static/theme.js") as response:
            self.assertEqual(response.status_code, 200)

    def test_login_csrf_cookie_rotation_and_logout(self):
        old = self.token()
        self.assertEqual(self.client.post("/login", data={"csrf": "bad"}).status_code, 400)
        response = self.login()
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(flag, cookie)
        page = self.client.get("/movies", base_url="https://localhost")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Cerrar sesi", page.data)
        with self.client.session_transaction() as session:
            token = session["sync_csrf"]
        self.assertNotEqual(token, old)
        self.assertEqual(self.client.post("/logout", base_url="https://localhost").status_code, 400)
        self.assertEqual(self.client.post("/logout", base_url="https://localhost",
                                         data={"csrf": token}).status_code, 303)
        self.assertEqual(self.client.get("/movies").location, "/login")

    def test_generic_error_and_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.login("wrong").status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")

    def test_expiry_and_persistent_secret(self):
        self.login()
        with self.client.session_transaction() as session:
            session["login_time"] = time.time() - 12 * 60 * 60 - 1
        self.assertEqual(self.client.get("/movies").location, "/login")
        self.assertEqual(create_app(self.root / "second.db").secret_key, self.app.secret_key)

    def test_invalid_or_missing_explicit_config_fails_closed(self):
        for content in ("{}", "[]", "invalid", json.dumps({**self.config, "secret_key": "short"})):
            self.path.write_text(content)
            with self.assertRaises(RuntimeError):
                create_app(self.root / "invalid.db")
        self.path.unlink()
        with self.assertRaises(RuntimeError):
            create_app(self.root / "missing.db")

    def test_login_does_not_accept_external_redirect(self):
        response = self.client.get("/login?next=https://example.invalid")
        self.assertNotIn(b"example.invalid", response.data)
        self.assertEqual(self.login().location, "/movies")
