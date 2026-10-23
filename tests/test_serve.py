import os
import unittest
from unittest.mock import patch

from app import DEFAULT_DB, main


class ServeTests(unittest.TestCase):
    def test_serve_uses_local_database_and_reload_without_debugger(self):
        with patch.dict(os.environ), patch("app.CatalogStore"), patch("app.create_app") as factory:
            os.environ.pop("IPTV_DB_PATH", None)
            self.assertEqual(main(["serve"]), 0)
        factory.assert_called_once_with(str(DEFAULT_DB))
        factory.return_value.config.__setitem__.assert_called_with("TEMPLATES_AUTO_RELOAD", True)
        factory.return_value.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False, use_reloader=True
        )

    def test_serve_preserves_explicit_database_port_and_reload_opt_out(self):
        with patch("app.CatalogStore"), patch("app.create_app") as factory:
            self.assertEqual(main(["--db", "custom.db", "serve", "--port", "5050", "--no-reload"]), 0)
        factory.assert_called_once_with("custom.db")
        factory.return_value.run.assert_called_once_with(
            host="127.0.0.1", port=5050, debug=False, use_reloader=False
        )

    def test_serve_preserves_environment_database(self):
        with patch.dict(os.environ, {"IPTV_DB_PATH": "environment.db"}):
            with patch("app.CatalogStore"), patch("app.create_app") as factory:
                self.assertEqual(main(["serve"]), 0)
        factory.assert_called_once_with("environment.db")
