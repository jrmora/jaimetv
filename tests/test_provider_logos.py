import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import render_template

from app import create_app
from provider_logos import download_logo, provider_icons, sync_logos
from tmdb import TMDBError


class ProviderLogoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "providers"
        self.directory.mkdir()

    def test_name_mapping_groups_plans_and_reuses_canonical_logo(self):
        (self.directory / "disney.jpg").write_bytes(b"image")
        (self.directory / "paramount.jpg").write_bytes(b"image")
        entries = {
            "Disney Plus": "disney.jpg",
            "Paramount Plus": "paramount.jpg",
            "Paramount+ Amazon Channel": "missing.jpg",
            "Unsafe": "../outside.jpg",
        }
        (self.directory / "manifest.json").write_text(json.dumps(entries))
        icons = provider_icons({
            "flatrate": ["Disney Plus", "Paramount Plus", "Paramount+ Amazon Channel", "Unknown", "Unsafe"],
            "buy": ["Disney Plus"],
        }, self.directory)
        self.assertEqual(icons["flatrate"][0]["filename"], "providers/disney.jpg")
        self.assertEqual(icons["flatrate"][1]["filename"], "providers/paramount.jpg")
        self.assertIsNone(icons["flatrate"][2]["filename"])
        self.assertIsNone(icons["flatrate"][3]["filename"])
        self.assertEqual(len(icons["flatrate"]), 4)
        self.assertEqual([item["name"] for item in icons["flatrate"]], [
            "Disney Plus", "Paramount Plus", "Unknown", "Unsafe",
        ])
        self.assertEqual(icons["buy"][0]["name"], "Disney Plus")
        self.assertEqual(provider_icons({"rent": ["New"]}, Path(self.temp.name))["rent"][0]["filename"], None)

    def test_sync_merges_movie_and_tv_catalogs_and_deduplicates_images(self):
        client = Mock()
        client.get.side_effect = [
            {"results": [{"provider_name": "One", "logo_path": "/same.jpg"}]},
            {"results": [{"provider_name": "Two", "logo_path": "/same.jpg"}, {"provider_name": "Three", "logo_path": "/other.png"}]},
        ]
        with patch("provider_logos.download_logo", side_effect=lambda path, directory: path[1:]) as download:
            names, images, errors = sync_logos(client, self.directory)
        self.assertEqual((names, images, errors), (3, 2, []))
        self.assertEqual(download.call_count, 2)
        manifest = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(manifest["One"], manifest["Two"])
        self.assertEqual(client.get.call_args_list[0].args[0], "watch/providers/movie")
        self.assertEqual(client.get.call_args_list[1].args[0], "watch/providers/tv")
        self.assertNotIn("watch_region", client.get.call_args.args[1])

    def test_grouped_icon_can_fall_back_to_an_existing_variant(self):
        (self.directory / "variant.png").write_bytes(b"image")
        (self.directory / "base.png").write_bytes(b"image")
        entries = {"Netflix Standard with Ads": "variant.png"}
        manifest = self.directory / "manifest.json"
        manifest.write_text(json.dumps(entries))
        icons = provider_icons({"flatrate": ["Netflix", "Netflix Kids"]}, self.directory)
        self.assertEqual(icons["flatrate"], [{"name": "Netflix", "filename": "providers/variant.png"}])
        entries["Netflix"] = "base.png"
        manifest.write_text(json.dumps(entries))
        self.assertEqual(provider_icons({"flatrate": ["Netflix Kids"]}, self.directory)["flatrate"],
                         [{"name": "Netflix", "filename": "providers/base.png"}])

    def test_failed_downloads_are_reported_without_broken_manifest_entries(self):
        client = Mock()
        client.get.return_value = {"results": [{"provider_name": "Broken", "logo_path": "/bad.jpg"}]}
        with patch("provider_logos.download_logo", side_effect=TMDBError("Download failed")):
            names, images, errors = sync_logos(client, self.directory)
        self.assertEqual((names, images), (0, 0))
        self.assertEqual(len(errors), 1)
        self.assertIn("Download failed", errors[0])
        self.assertEqual(json.loads((self.directory / "manifest.json").read_text()), {})

    def test_download_validation_tls_and_reuse(self):
        content = b"\xff\xd8\xff" + b"image"
        with patch("provider_logos.sys.platform", "darwin"), patch(
            "provider_logos.subprocess.run", return_value=Mock(returncode=0, stdout=content)
        ) as run:
            self.assertEqual(download_logo("/safe.jpg", self.directory), "safe.jpg")
            self.assertEqual(download_logo("/safe.jpg", self.directory), "safe.jpg")
        run.assert_called_once()
        self.assertNotIn("--insecure", run.call_args.args[0])
        self.assertNotIn("-k", run.call_args.args[0])
        self.assertEqual((self.directory / "safe.jpg").read_bytes(), content)
        for path in ("https://evil.invalid/a.jpg", "/../a.jpg", "/a.svg", None):
            with self.assertRaises(TMDBError):
                download_logo(path, self.directory)
        with patch("provider_logos.sys.platform", "linux"), patch(
            "provider_logos.requests.get", return_value=Mock(status_code=200, content=b"<html>error</html>")
        ):
            with self.assertRaises(TMDBError):
                download_logo("/invalid.jpg", self.directory)
        self.assertFalse((self.directory / "invalid.jpg").exists())

    def test_template_uses_local_icons_and_accessible_names_with_text_fallback(self):
        app = create_app(Path(self.temp.name) / "test.db")
        data = {
            "status": "matched", "checked_at": "2026-10-22", "media": "movie", "id": 63,
            "providers": {"flatrate": ["Disney Plus", "<Unknown>"], "rent": [], "buy": [], "free": [], "ads": []},
        }
        icons = {"flatrate": [
            {"name": "Disney Plus", "filename": "providers/disney.jpg"},
            {"name": "<Unknown>", "filename": None},
        ]}
        with app.test_request_context():
            html = render_template("tmdb.html", tmdb=data, provider_icons=icons)
        self.assertIn('src="/static/providers/disney.jpg"', html)
        self.assertIn('alt="Disney Plus"', html)
        self.assertIn('title="Disney Plus"', html)
        self.assertIn('tabindex="0" aria-label="Disney Plus"', html)
        self.assertIn("&lt;Unknown&gt;", html)
        self.assertNotIn("<Unknown>", html)
        self.assertIn("Sin disponibilidad confirmada", html)


if __name__ == "__main__":
    unittest.main()
