import copy
import json
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from app import create_app
from catalog import CatalogStore, fingerprint
from test_app import snapshot
from tmdb import TMDBError
from platforms import platform_name, ordered_platforms


class ProviderFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        self.services_path = Path(self.temp.name) / "services.json"
        self.set_services([])
        config_patch = patch("catalog.SERVICES_PATH", self.services_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.app = create_app(self.path)
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.data = snapshot()
        self.store.replace(self.data)
        self.client = self.app.test_client()
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.csrf = session["sync_csrf"]

    def set_services(self, names):
        self.services_path.write_text(json.dumps({"services": names}), encoding="utf-8")

    def cache(self, kind, record_id, providers, status="matched", country="CA"):
        raw = self.store.detail(kind, str(record_id))["data"]
        self.store.save_tmdb(kind, str(record_id), country, fingerprint(raw), {
            "status": status, "providers": providers, "id": 63, "country": country,
        })

    def ids(self, **kwargs):
        return {item["id"] for item in self.store.query("movies", **kwargs)["items"]}

    def test_tmdb_status_filter_uses_current_canadian_cache(self):
        self.cache("movies", 10, {}, status="candidates")
        self.cache("movies", 11, {}, status="missing")
        self.cache("movies", 12, {"flatrate": ["Netflix"]})
        self.assertEqual(self.ids(tmdb_status="candidates"), {"10"})
        self.assertEqual(self.ids(tmdb_status="missing"), {"11"})
        self.assertEqual(self.ids(tmdb_status="matched"), {"12"})
        self.assertEqual(self.ids(tmdb_status="pending"), {"13"})
        self.assertEqual(self.ids(tmdb_status="all"), {"10", "11", "12", "13"})
        self.assertEqual(self.ids(tmdb_status="candidates", q="bat", category="1"), {"10"})
        self.assertEqual(self.ids(tmdb_status="candidates", provider=["Netflix"]), set())
        self.cache("movies", 13, {}, status="candidates", country="US")
        self.assertEqual(self.ids(tmdb_status="pending"), {"13"})
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "Changed"
        self.store.replace(changed)
        self.assertEqual(self.ids(tmdb_status="candidates"), set())
        self.assertEqual(self.ids(tmdb_status="pending"), {"10", "13"})

    def test_tmdb_status_ui_api_detail_links_and_resolution(self):
        self.cache("movies", 10, {}, status="candidates")
        html = self.client.get("/movies?tmdb_status=candidates").get_data(as_text=True)
        self.assertIn('id="tmdb-status"', html)
        self.assertIn('value="candidates" selected', html)
        self.assertIn("Por elegir", html)
        self.assertIn("tmdb_status=candidates", html)
        payload = self.client.get("/api/catalog/movies?view=cards&tmdb_status=candidates").json
        self.assertEqual(payload["total"], 1)
        self.assertIn("tmdb_status=candidates", payload["html"])
        detail = self.client.get("/catalog/movies/detail?id=10&tmdb_status=candidates").get_data(as_text=True)
        self.assertIn("tmdb_status=candidates", detail)
        self.cache("movies", 10, {"flatrate": ["Netflix"]})
        self.assertEqual(self.ids(tmdb_status="candidates"), set())
        self.assertEqual(self.ids(tmdb_status="matched"), {"10"})
        self.cache("series", 20, {}, status="candidates")
        self.assertEqual(self.store.query("series", tmdb_status="candidates")["total"], 1)
        for path in ("/series", "/kids/movies", "/kids/series", "/movies?foreign=1"):
            self.assertIn('id="tmdb-status"', self.client.get(path).get_data(as_text=True))
        self.assertNotIn('id="tmdb-status"', self.client.get("/channels").get_data(as_text=True))
        for path in ("/movies?tmdb_status=bad", "/api/catalog/movies?tmdb_status=bad",
                     "/channels?tmdb_status=candidates"):
            self.assertEqual(self.client.get(path).status_code, 400)
        with self.assertRaises(ValueError):
            self.store.query("channels", tmdb_status="candidates")

    def test_candidate_filter_pagination_and_banned_movies(self):
        data = snapshot()
        data["movies"] = [{"stream_id": i, "name": f"Movie {i:03}"} for i in range(55)]
        self.store.replace(data)
        for number in range(55):
            self.cache("movies", number, {}, status="candidates")
        self.store.set_movie_banned("0", True)
        payload = self.client.get("/api/catalog/movies?view=cards&tmdb_status=candidates&page=2").json
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (54, 2, 4))
        self.assertIn("tmdb_status=candidates", payload["html"])
        self.assertEqual(self.store.tmdb_coverage("movies")["candidates"], 55)

    def test_provider_or_access_and_subscription_intersection(self):
        self.cache("movies", 10, {"flatrate": ["Netflix"], "rent": ["YouTube"]})
        self.cache("movies", 11, {"flatrate": ["Disney Plus"]})
        self.cache("movies", 12, {"rent": ["Netflix"]})
        self.assertEqual(self.ids(provider=["Netflix"]), {"10"})
        self.assertEqual(self.ids(provider=["Netflix", "Disney Plus"]), {"10", "11"})
        self.assertEqual(self.ids(provider=["YouTube"], access="rent"), {"10"})
        self.assertEqual(self.ids(provider=["Netflix"], access="all"), {"10", "12"})
        self.assertEqual(self.ids(provider=["Netflix"], category="2"), set())
        self.assertEqual(self.ids(provider=["Netflix"], q="bat"), {"10"})
        self.assertEqual(self.ids(my_services=True), set())
        self.set_services(["Netflix"])
        self.assertEqual(self.ids(my_services=True), {"10"})
        self.assertEqual(self.ids(provider=["Disney Plus"], my_services=True), set())
        self.assertEqual(self.ids(provider=["Disney Plus", "Netflix"], my_services=True), {"10"})

    def test_unconfirmed_other_country_or_stale_cache_never_matches(self):
        self.cache("movies", 10, {"flatrate": ["Netflix"]}, country="US")
        self.cache("movies", 11, {"flatrate": ["Netflix"]}, status="candidates")
        self.cache("movies", 12, {"flatrate": ["Netflix"]}, status="missing")
        self.assertEqual(self.ids(provider=["Netflix"]), set())
        self.cache("movies", 10, {"flatrate": ["Netflix"]})
        self.assertEqual(self.ids(provider=["Netflix"]), {"10"})
        changed = copy.deepcopy(self.data)
        changed["movies"][0]["name"] = "Another movie"
        self.store.replace(changed)
        self.assertEqual(self.ids(provider=["Netflix"]), set())
        self.assertNotIn("Netflix", self.store.provider_names("movies"))
        self.assertEqual(self.store.tmdb_coverage("movies"), {
            "total": 4, "checked": 2, "matched": 0, "candidates": 1, "missing": 1, "pending": 2,
        })

    def test_brand_aliases_and_paid_channels_are_grouped_without_becoming_prime(self):
        for name in ("Netflix", "Netflix Kids", "Netflix Standard with Ads"):
            self.assertEqual(platform_name(name), "Netflix")
        for name in ("Amazon Prime Video", "Amazon Prime Video with Ads", "Amazon Video"):
            self.assertEqual(platform_name(name), "Amazon Prime Video")
        for name, brand in (
            ("Paramount+ Amazon Channel", "Paramount Plus"),
            ("Paramount Plus Premium", "Paramount Plus"),
            ("Apple TV Store", "Apple TV"),
            ("AcornTV Amazon Channel", "Acorn TV"),
            ("Britbox Apple TV channel", "BritBox"),
            ("AMC Plus Apple TV channel", "AMC+"),
        ):
            self.assertEqual(platform_name(name), brand)
        self.set_services(["Netflix", "Amazon Prime Video"])
        self.cache("movies", 10, {"flatrate": ["Netflix Standard with Ads", "Netflix Kids"]})
        self.cache("movies", 11, {"flatrate": ["Amazon Prime Video with Ads"]})
        self.cache("movies", 12, {"flatrate": ["Paramount+ Amazon Channel"]})
        self.assertEqual(self.ids(my_services=True), {"10", "11"})
        self.assertEqual(self.ids(provider=["Netflix Standard with Ads"]), {"10"})
        self.assertEqual(self.ids(provider=["Paramount Plus"]), {"12"})
        items = self.store.query("movies", my_services=True)["items"]
        self.assertEqual(next(item for item in items if item["id"] == "10")["matched_services"], ["Netflix"])
        self.assertEqual(self.store.detail("movies", "10")["data"], self.data["movies"][0])

    def test_priority_order_and_single_icon_in_initial_and_async_cards(self):
        self.set_services(["Netflix", "Amazon Prime Video"])
        self.store.save_provider_directory({"movies": [
            "Zed", "Apple TV Store", "Netflix Standard with Ads", "Amazon Prime Video with Ads",
            "Paramount Plus Premium", "Disney Plus", "Netflix Kids", "AcornTV Amazon Channel",
        ]})
        names = ordered_platforms(self.store.provider_names("movies") + self.store.subscriptions())
        self.assertEqual(names, [
            "Netflix", "Amazon Prime Video", "Apple TV", "Disney Plus", "Paramount Plus", "Acorn TV", "Zed",
        ])
        html = self.client.get("/movies?provider=Netflix+Kids").get_data(as_text=True)
        self.assertEqual(re.findall(r'data-provider-name="([^"]+)"', html), names)
        self.assertNotIn("Netflix Kids", html)
        self.assertIn('value="Netflix" checked', html)
        self.cache("movies", 10, {"flatrate": ["Netflix", "Netflix Kids", "Netflix Standard with Ads"]})
        for path in ("/movies?my_services=1", "/api/catalog/movies?view=cards&my_services=1"):
            response = self.client.get(path)
            html = response.json["html"] if path.startswith("/api") else response.get_data(as_text=True)
            self.assertEqual(html.count('class="card-service-logo rounded"'), 1)
            self.assertNotIn("Netflix Standard with Ads", html)

    def test_outside_services_excludes_unconfirmed_and_titles_on_either_service(self):
        self.set_services(["Netflix", "Amazon Prime Video"])
        self.cache("movies", 10, {"flatrate": ["Netflix Standard with Ads", "Disney Plus"]})
        self.cache("movies", 11, {"flatrate": ["Amazon Prime Video with Ads"]})
        self.cache("movies", 12, {"flatrate": ["Disney Plus"]})
        self.cache("movies", 13, {"flatrate": ["Disney Plus"]}, status="candidates")
        self.assertEqual(self.ids(my_services=2), {"12"})
        self.assertEqual(self.ids(my_services=2, provider=["Disney Plus"]), {"12"})
        self.assertEqual(self.ids(my_services=2, provider=["Netflix"]), set())
        self.assertEqual(self.ids(my_services=2, q="literal"), {"12"})
        self.assertEqual(self.ids(my_services=2, category="1"), set())
        payload = self.client.get("/api/catalog/movies?view=cards&my_services=2").json
        self.assertEqual(payload["total"], 1)
        self.assertNotIn('class="card-services', payload["html"])
        self.assertIn("my_services=2", payload["html"])
        self.assertNotIn("matched_services", payload["items"][0])

    def test_outside_services_respects_country_fingerprint_status_and_access(self):
        self.set_services(["Netflix"])
        self.cache("movies", 10, {"rent": ["Netflix"], "flatrate": ["Crave"]})
        self.cache("movies", 11, {"flatrate": ["Crave"]}, country="US")
        self.cache("movies", 12, {}, status="missing")
        self.cache("movies", 13, {"flatrate": ["Crave"]})
        changed = copy.deepcopy(self.data)
        changed["movies"][3]["name"] = "Changed identity"
        self.store.replace(changed)
        self.assertEqual(self.ids(my_services=2), {"10"})
        self.assertEqual(self.ids(my_services=2, access="rent"), set())
        self.assertEqual(self.ids(my_services=2, access="all"), set())
        self.cache("movies", 12, {})
        self.assertEqual(self.ids(my_services=2), {"10", "12"})
        self.assertEqual(self.client.get("/channels?my_services=2").status_code, 400)
        self.assertEqual(self.client.get("/movies?my_services=3").status_code, 400)
        self.assertEqual(self.client.get("/api/catalog/channels?my_services=2").status_code, 400)
        with self.assertRaises(ValueError):
            self.store.query("channels", my_services=2)

    def test_outside_services_global_pagination_and_series(self):
        self.set_services(["Netflix"])
        data = snapshot()
        data["movies"] = [{"stream_id": i, "name": f"Movie {i:03}"} for i in range(60)]
        self.store.replace(data)
        for number in range(60):
            self.cache("movies", number, {"flatrate": ["Crave"]})
        self.cache("movies", 0, {"flatrate": ["Netflix Kids", "Crave"]})
        payload = self.client.get("/api/catalog/movies?view=cards&my_services=2&page=2").json
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (59, 2, 9))
        self.assertIn("my_services=2", payload["html"])
        self.cache("series", 20, {"flatrate": ["Crave"]})
        self.assertEqual(self.store.query("series", my_services=2)["total"], 1)
        self.cache("series", 20, {"flatrate": ["Netflix Standard with Ads"]})
        self.assertEqual(self.store.query("series", my_services=2)["total"], 0)

    def test_directory_is_country_and_kind_specific_and_groups_variants(self):
        self.store.save_provider_directory({
            "movies": ["Netflix", "Paramount Plus", "Paramount+ Amazon Channel"],
            "series": ["Crave"],
        })
        self.store.save_provider_directory({"movies": ["Only US"]}, "US")
        self.cache("movies", 10, {"flatrate": ["Local provider"]})
        self.assertEqual(self.store.provider_names("movies"), [
            "Local provider", "Netflix", "Paramount Plus",
        ])
        self.assertEqual(self.store.provider_names("series"), ["Crave"])

    def test_subscriptions_come_from_file_without_edit_buttons_and_escape_names(self):
        endpoint = "/api/providers/subscriptions"
        self.assertEqual(self.client.post(endpoint, data={"provider": "Netflix"}).status_code, 404)
        names = ["Netflix", "Amazon Prime Video", '<script>alert("provider")</script>']
        self.set_services(names)
        self.assertEqual(set(CatalogStore(self.path).subscriptions()), set(names))
        html = self.client.get("/movies?my_services=1").get_data(as_text=True)
        self.assertNotIn('<script>alert("provider")</script>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Amazon Prime Video", html)
        for button in ("save-services", "select-services", "clear-providers"):
            self.assertNotIn(f'id="{button}"', html)
        for names in (["x" * 201], [""], [False], "Netflix"):
            self.set_services(names)
            with self.assertLogs(self.app.logger, level="WARNING"):
                response = self.client.get("/movies")
            self.assertEqual(response.status_code, 500)
            self.assertIn("services.json", response.get_data(as_text=True))

    def test_other_platform_filter_does_not_change_fixed_services(self):
        self.set_services(["Netflix", "Amazon Prime Video"])
        self.cache("movies", 10, {"flatrate": ["Netflix"]})
        self.cache("movies", 11, {"flatrate": ["Amazon Prime Video"]})
        self.cache("movies", 12, {"flatrate": ["Crave"]})
        self.assertEqual(self.ids(my_services=True), {"10", "11"})
        self.assertEqual(self.ids(provider=["Crave"]), {"12"})
        self.assertEqual(self.ids(provider=["Crave"], my_services=True), set())
        response = self.client.get("/api/catalog/movies?provider=Crave&my_services=0")
        self.assertEqual([item["id"] for item in response.json["items"]], ["12"])
        self.assertEqual(self.store.subscriptions(), ["Amazon Prime Video", "Netflix"])

    def test_subscription_card_logos_match_services_access_and_selected_platforms(self):
        self.set_services(["Netflix", "Amazon Prime Video"])
        self.cache("movies", 10, {
            "flatrate": ["Netflix", "Amazon Prime Video", "Crave"],
            "rent": ["Netflix"],
        })
        self.cache("movies", 11, {"flatrate": ["Netflix"]})
        items = self.store.query("movies", my_services=True)["items"]
        batman = next(item for item in items if item["id"] == "10")
        self.assertEqual(batman["matched_services"], ["Amazon Prime Video", "Netflix"])
        for endpoint in ("/movies", "/api/catalog/movies"):
            response = self.client.get(endpoint + "?my_services=1&view=cards")
            html = response.json["html"] if endpoint.startswith("/api/") else response.get_data(as_text=True)
            cards = re.findall(r'<article class="catalog-card.*?</article>', html, re.S)
            self.assertEqual(len(cards), 2)
            batman_card = next(card for card in cards if "Batman" in card)
            self.assertEqual(batman_card.count('class="card-service-logo'), 2)
            self.assertIn('alt="Netflix"', batman_card)
            self.assertIn('alt="Amazon Prime Video"', batman_card)
            self.assertNotIn("Crave", batman_card)
            self.assertIn('width="24" height="24"', batman_card)
            single_card = next(card for card in cards if "Batman" not in card)
            self.assertEqual(single_card.count('class="card-service-logo'), 1)
        for query in ("my_services=1&provider=Netflix", "my_services=1&access=rent"):
            result = self.client.get("/api/catalog/movies?view=cards&" + query).json
            self.assertNotIn('alt="Amazon Prime Video"', result["html"])
            self.assertIn('alt="Netflix"', result["html"])
        self.assertNotIn("card-service-logo", self.client.get("/movies").get_data(as_text=True))
        self.assertNotIn("card-service-logo", self.client.get("/movies?provider=Netflix").get_data(as_text=True))

    def test_subscription_cards_fallback_to_names_when_logo_is_missing(self):
        self.set_services(["Unknown platform"])
        self.cache("movies", 10, {"flatrate": ["Unknown platform"]})
        result = self.client.get("/api/catalog/movies?view=cards&my_services=1").json
        self.assertIn('class="card-services', result["html"])
        self.assertIn("Unknown platform", result["html"])
        self.assertNotIn("card-service-logo", result["html"])

    def test_filter_on_every_movie_series_listing_not_channels(self):
        for url in ("/movies", "/series", "/kids/movies", "/kids/series", "/movies?foreign=1", "/series?foreign=1"):
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                self.assertIn('id="provider-filter"', html)
                self.assertIn("Canadá (CA)", html)
                self.assertIn('id="my-services-toggle"', html)
                self.assertIn('form="tmdb-batch-controls"', html)
                self.assertIn('<form id="tmdb-batch-controls">', html)
                self.assertIn("pendientes", html)
        self.assertNotIn('id="provider-filter"', self.client.get("/channels").get_data(as_text=True))
        for query in ("provider=Netflix", "my_services=1"):
            self.assertEqual(self.client.get(f"/api/catalog/channels?{query}").status_code, 400)
        for query in ("access=invalid", "my_services=true", "provider=" + "x" * 201):
            self.assertEqual(self.client.get("/api/catalog/movies?" + query).status_code, 400)

    def test_global_pagination_and_back_sort_links_preserve_filters(self):
        data = snapshot()
        data["movies"] = [{"stream_id": i, "name": f"Movie {i:03}", "year": "2000"} for i in range(60)]
        self.store.replace(data)
        for item in data["movies"]:
            self.cache("movies", item["stream_id"], {"flatrate": ["Netflix"]})
        self.set_services(["Netflix"])
        query = "provider=Netflix&my_services=1&access=flatrate&view=cards&page=2"
        result = self.client.get("/api/catalog/movies?" + query).json
        self.assertEqual((result["total"], result["page"], len(result["items"])), (60, 2, 10))
        html = self.client.get("/movies?" + query).get_data(as_text=True)
        links = [unescape(url) for url in re.findall(r'href="([^"]+)"', html)]
        detail = next(url for url in links if "/catalog/movies/detail?" in url)
        pagination = next(url for url in links if "/movies?" in url and "page=1" in url)
        for url in (detail, pagination):
            values = parse_qs(urlsplit(url).query)
            self.assertEqual(values["provider"], ["Netflix"])
            self.assertEqual(values["my_services"], ["1"])
            self.assertEqual(values["access"], ["flatrate"])
        html = self.client.get(detail).get_data(as_text=True)
        back = unescape(re.search(r'id="back-button"[^>]*href="([^"]+)"', html)[1])
        self.assertEqual(parse_qs(urlsplit(back).query)["provider"], ["Netflix"])
        self.assertEqual(parse_qs(urlsplit(back).query)["page"], ["2"])


class TMDBBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "catalog.db")
        self.app.testing = True
        self.store = self.app.config["CATALOG_STORE"]
        self.store.replace(snapshot())
        self.job = self.app.config["TMDB_BATCH"]
        self.remote = Mock()
        self.remote.get.return_value = {"results": [{"provider_name": "Netflix"}, {"provider_name": "Crave"}]}
        self.remote.search.return_value = ([], 63)
        self.remote.details.return_value = {"status": "matched", "id": 63, "providers": {"flatrate": ["Netflix"]}}
        self.factory = Mock(return_value=self.remote)
        self.app.config["TMDB_CLIENT_FACTORY"] = self.factory
        self.client = self.app.test_client()
        self.client.get("/movies")
        with self.client.session_transaction() as session:
            self.token = session["sync_csrf"]
        self.thread_patch = patch("tmdb_jobs.threading.Thread")
        self.thread = self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def run_job(self, kind="movies", limit=2):
        self.assertTrue(self.job.start(kind, limit, self.factory))
        with patch.object(self.job.cancelled, "wait", return_value=False):
            self.job.run(kind, limit, self.factory)

    def test_bounded_batch_saves_and_resumes_without_requerying_cached_titles(self):
        original = self.store.detail("movies", "10")
        self.run_job()
        state = self.job.status()
        self.assertEqual((state["status"], state["processed"], state["matched"]), ("completed", 2, 2))
        first = {call.args[1]["stream_id"] for call in self.remote.search.call_args_list}
        self.assertEqual(self.store.tmdb_coverage("movies")["pending"], 2)
        self.assertEqual(self.store.provider_names("movies"), ["Crave", "Netflix"])
        self.run_job(limit=5)
        self.assertEqual(self.job.status()["processed"], 2)
        self.assertEqual(self.remote.search.call_count, 4)
        self.assertEqual(self.store.tmdb_coverage("movies")["pending"], 0)
        self.assertEqual(self.store.detail("movies", "10"), original)
        self.remote.search.reset_mock()
        self.run_job()
        self.remote.search.assert_not_called()
        self.assertEqual(self.job.status()["processed"], 0)
        self.assertEqual(len(first), 2)
        for call in self.remote.get.call_args_list:
            self.assertEqual(call.args[1]["watch_region"], "CA")

    def test_ambiguity_and_no_match_saved_without_automatic_selection(self):
        self.remote.search.side_effect = [([{"id": 1}], None), ([], None)]
        self.run_job()
        state = self.job.status()
        self.assertEqual((state["candidates"], state["missing"], state["matched"]), (1, 1, 0))
        self.remote.details.assert_not_called()
        self.assertEqual(self.store.tmdb_coverage("movies")["pending"], 2)
        self.assertEqual(self.store.query("movies", provider=["Netflix"])["total"], 0)

    def test_cancel_after_current_title_and_reject_second_start(self):
        self.assertTrue(self.job.start("movies", 4, self.factory))
        self.assertFalse(self.job.start("series", 5, self.factory))

        def search(*args):
            self.job.cancel()
            return [], 63

        self.remote.search.side_effect = search
        self.job.run("movies", 4, self.factory)
        self.assertEqual(self.job.status()["status"], "cancelled")
        self.assertEqual(self.job.status()["processed"], 1)
        self.assertEqual(self.store.tmdb_coverage("movies")["pending"], 3)

    def test_error_stops_with_partial_results_and_safe_message(self):
        self.remote.search.side_effect = [([], 63), TMDBError("TMDB respondió HTTP 429.")]
        self.run_job(limit=4)
        self.assertEqual(self.job.status()["status"], "failed")
        self.assertEqual(self.job.status()["processed"], 1)
        self.assertIn("429", self.job.status()["error"])
        self.assertEqual(self.store.tmdb_coverage("movies")["pending"], 3)
        self.remote.search.side_effect = RuntimeError("private-token")
        self.run_job()
        self.assertNotIn("private-token", self.job.status()["error"])

    def test_directory_only_and_malformed_directory_preserves_previous(self):
        self.run_job(limit=0)
        self.remote.search.assert_not_called()
        self.assertEqual(self.store.provider_names("series"), ["Crave", "Netflix"])
        self.remote.get.return_value = {"results": [{"provider_name": 123}]}
        self.run_job(limit=0)
        self.assertEqual(self.job.status()["status"], "failed")
        self.assertEqual(self.store.provider_names("series"), ["Crave", "Netflix"])

    def test_route_validation_csrf_status_and_cancel(self):
        endpoint = "/api/tmdb/batch"
        self.assertEqual(self.client.get(endpoint).json["status"], "idle")
        self.assertEqual(self.client.post(endpoint, data={"kind": "movies"}).status_code, 400)
        for values in ({"kind": "channels"}, {"kind": "movies", "limit": "0"},
                       {"kind": "movies", "limit": "501"}, {"kind": "movies", "limit": "1.5"},
                       {"kind": "movies", "action": "unknown"}):
            self.assertEqual(self.client.post(endpoint, data={"csrf": self.token, **values}).status_code, 400)
        data = {"csrf": self.token, "kind": "movies", "limit": "2"}
        self.assertEqual(self.client.post(endpoint, data=data).status_code, 202)
        self.assertEqual(self.client.get(endpoint).json["status"], "running")
        self.assertEqual(self.client.post(endpoint, data=data).status_code, 409)
        self.assertEqual(self.client.get(endpoint).headers["Cache-Control"], "no-store")
        self.client.post(endpoint, data={"csrf": self.token, "action": "cancel"})
        self.assertTrue(self.job.cancelled.is_set())
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
