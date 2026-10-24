import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from catalog import fingerprint
from test_app import snapshot
from test_tmdb import movie_details
from tmdb import TMDBClient


class TrailerPlayerTests(unittest.TestCase):
    def test_video_language_preference_and_english_fallback(self):
        client = object.__new__(TMDBClient)
        data = movie_details()
        data["videos"]["results"] = [
            {"site": "YouTube", "type": "Trailer", "key": "abcdefghijk", "official": True, "iso_639_1": "en"},
            {"site": "YouTube", "type": "Trailer", "key": "nsuk8ThvnpM", "official": True, "iso_639_1": "es"},
        ]
        with patch.object(client, "get", return_value=data) as get:
            result = client.details("movies", 63)
            self.assertTrue(result["trailer_url"].endswith("nsuk8ThvnpM"))
            self.assertEqual(get.call_count, 1)
        data["videos"]["results"] = []
        with patch.object(client, "get", side_effect=[data, {"results": [
            {"site": "YouTube", "type": "Trailer", "key": "abcdefghijk", "official": True},
        ]}]) as get:
            result = client.details("series", 63)
            self.assertTrue(result["trailer_url"].endswith("abcdefghijk"))
            self.assertEqual(get.call_args.args, ("tv/63/videos", {"language": "en-US"}))

    def test_cached_trailer_is_lazy_and_invalid_urls_are_not_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "catalog.db")
            store = app.config["CATALOG_STORE"]
            store.replace(snapshot())
            raw = store.detail("movies", "10")["data"]
            client = app.test_client()
            for url, valid in (("https://www.youtube.com/watch?v=nsuk8ThvnpM", True),
                               ("https://evil.example/watch?v=nsuk8ThvnpM", False)):
                store.save_tmdb("movies", "10", "CA", fingerprint(raw), {
                    "status": "matched", "id": 63, "media": "movie", "title": "Movie",
                    "genres": [], "countries": [], "companies": [], "trailer_url": url,
                })
                response = client.get("/api/tmdb/movies/10")
                self.assertEqual(response.status_code, 200)
                self.assertEqual('data-video-id="nsuk8ThvnpM"' in response.json["html"], valid)
                self.assertNotIn("<iframe", response.json["html"])
            policy = client.get("/catalog/movies/detail?id=10").headers["Content-Security-Policy"]
            self.assertIn("frame-src https://www.youtube-nocookie.com;", policy)
