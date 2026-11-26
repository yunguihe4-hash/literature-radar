import json
import os
import time
import unittest
from unittest.mock import patch
from literature_radar.sources.http import HTTPClient, HTTPResponse, RequestFailure
from literature_radar.sources.factory import make_provider
from tests.mock_api import FakeTransport

class HTTPTests(unittest.TestCase):
    def test_direct_by_default_and_proxy_connect_opt_in(self):
        calls = []

        class Response:
            status = 200

            def getheaders(self):
                return []

            def read(self, _size):
                return b""

        class Connection:
            def __init__(self, host, port, **_kwargs):
                calls.append(("connection", host, port))
                self.sock = self

            def set_tunnel(self, host, port):
                calls.append(("tunnel", host, port))

            def connect(self):
                calls.append(("connect",))

            def settimeout(self, _seconds):
                pass

            def request(self, method, target, headers):
                calls.append(("request", method, target, headers["User-Agent"]))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7892", "NO_PROXY": "",
                                  "LITERATURE_RADAR_OFFLINE_TEST": ""}), \
             patch("literature_radar.sources.http.http.client.HTTPSConnection", Connection):
            HTTPClient("crossref", max_retries=0).get("https://api.crossref.org/works", {"rows": 1})
            self.assertEqual(calls[0], ("connection", "api.crossref.org", None))
            self.assertFalse(any(call[0] == "tunnel" for call in calls))
            self.assertTrue(any(call[0] == "request" and call[2] == "/works?rows=1" for call in calls))
            calls.clear()
            HTTPClient("crossref", max_retries=0, use_environment_proxy=True).get("https://api.crossref.org/works")
            self.assertEqual(calls[:2], [("connection", "127.0.0.1", 7892),
                                         ("tunnel", "api.crossref.org", 443)])
            self.assertTrue(any(call[0] == "request" and call[2] == "/works" for call in calls))
            self.assertTrue(any(call[0] == "request" and call[3].startswith("literature-radar-core/")
                                and not call[3].endswith("/0.1") for call in calls))
            calls.clear()
            with patch.dict(os.environ, {"NO_PROXY": ".crossref.org"}):
                HTTPClient("crossref", max_retries=0, use_environment_proxy=True).get("https://api.crossref.org/works")
            self.assertEqual(calls[0], ("connection", "api.crossref.org", None))
            self.assertFalse(any(call[0] == "tunnel" for call in calls))
            calls.clear()
            HTTPClient("crossref", max_retries=0, proxy_url="http://localhost:8888").get("https://api.crossref.org/works")
            self.assertEqual(calls[:2], [("connection", "localhost", 8888),
                                         ("tunnel", "api.crossref.org", 443)])

    def test_contact_and_proxy_validation(self):
        client = HTTPClient("crossref", contact_email="research@example.org")
        self.assertIn("(mailto:research@example.org)", client.user_agent)
        for invalid in ("https://proxy.invalid:443", "http://user:password@proxy.invalid:8080",
                        "http://proxy.invalid/path"):
            with self.subTest(proxy=invalid), self.assertRaises(ValueError):
                HTTPClient("crossref", proxy_url=invalid)
        with self.assertRaises(ValueError):
            HTTPClient("crossref", contact_email="bad\ncontact@example.org")
        provider = make_provider("europepmc", options={
            "proxy_url": "http://localhost:7892", "use_environment_proxy": True,
            "contact_email": "research@example.org"})
        self.assertEqual(provider.client.proxy_url, "http://localhost:7892")
        self.assertTrue(provider.client.use_environment_proxy)
        self.assertIn("mailto:research@example.org", provider.client.user_agent)

    def test_retry_after_and_server_recovery(self):
        waits = []
        fake = FakeTransport([HTTPResponse(429, {"Retry-After": "2"}, b""),
                              HTTPResponse(503, {}, b""), {"ok": True}])
        client = HTTPClient("synthetic", transport=fake, sleeper=waits.append,
                            max_retries=2, rate_per_second=100000)
        self.assertEqual(client.get_json("https://example.invalid/test", {"q": "fiction"}), {"ok": True})
        self.assertTrue(any(wait >= 2 for wait in waits))
        self.assertEqual(len(fake.calls), 3)

    def test_nonretry_auth_invalid_json_and_safe_error(self):
        for status, category in ((400, "invalid_query_or_cursor"),
                                 (401, "authentication_or_permission"),
                                 (403, "authentication_or_permission")):
            with self.subTest(status=status):
                fake = FakeTransport([HTTPResponse(status, {}, b"private words")])
                client = HTTPClient("synthetic", transport=fake, sleeper=lambda _: None)
                with self.assertRaises(RequestFailure) as raised:
                    client.get("https://example.invalid", {"q": "private words"}, {"Authorization": "secret"})
                self.assertEqual(raised.exception.category, category)
                self.assertNotIn("private words", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(len(fake.calls), 1)
        fake = FakeTransport([HTTPResponse(200, {}, b"not json")])
        with self.assertRaisesRegex(RequestFailure, "invalid_json"):
            HTTPClient("synthetic", transport=fake, sleeper=lambda _: None).get_json("https://example.invalid")

    def test_timeout_connection_and_https_only(self):
        for error, category in ((TimeoutError(), "timeout"), (ConnectionError(), "connection")):
            fake = FakeTransport([error])
            with self.assertRaises(RequestFailure) as raised:
                HTTPClient("synthetic", transport=fake, sleeper=lambda _: None, max_retries=0).get("https://example.invalid")
            self.assertEqual(raised.exception.category, category)
            self.assertIsInstance(raised.exception.__cause__, type(error))
        with self.assertRaisesRegex(RequestFailure, "invalid_endpoint"):
            HTTPClient("synthetic", transport=lambda *_: None).get("http://example.invalid")

    def test_crossref_response_limit_can_only_slow_requests(self):
        fake = FakeTransport([HTTPResponse(200, {"x-rate-limit-limit": "2", "x-rate-limit-interval": "10s"}, b"{}")])
        client = HTTPClient("crossref", rate_per_second=1, transport=fake, sleeper=lambda _: None)
        client.get_json("https://example.invalid")
        self.assertEqual(client.interval, 5)
        self.assertEqual(client.last_status, 200)

    def test_retryable_502_504_and_network_disconnect_are_bounded(self):
        for failure in (HTTPResponse(502, {}, b""), HTTPResponse(504, {}, b""),
                        ConnectionError("synthetic disconnect")):
            with self.subTest(kind=type(failure).__name__):
                fake = FakeTransport([failure, {"ok": True}])
                client = HTTPClient("synthetic", transport=fake, sleeper=lambda _: None,
                                    max_retries=1)
                self.assertEqual(client.get_json("https://example.invalid"), {"ok": True})
                self.assertEqual(len(fake.calls), 2)

    def test_long_retry_after_defers_without_sleeping(self):
        waits = []
        fake = FakeTransport([HTTPResponse(429, {"Retry-After": "3600"}, b"")])
        client = HTTPClient("synthetic", transport=fake, sleeper=waits.append,
                            max_retries=2, max_retry_delay=30)
        with self.assertRaises(RequestFailure) as raised:
            client.get("https://example.invalid")
        self.assertEqual(raised.exception.category, "retry_later")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(waits, [])

    def test_query_group_budget_stops_retries_before_backoff(self):
        waits = []
        fake = FakeTransport([TimeoutError("fictional timeout"), {"ok": True}])
        client = HTTPClient("synthetic", transport=fake, sleeper=waits.append,
                            max_retries=2, rate_per_second=100000)
        client.budget_deadline = time.monotonic() + 0.05
        with self.assertRaises(RequestFailure) as raised:
            client.get_json("https://example.invalid")
        self.assertEqual(raised.exception.category, "budget_exhausted")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(waits, [])

    def test_query_group_budget_clamps_real_connection_timeout(self):
        timeouts = []

        class Response:
            status = 200

            def getheaders(self):
                return []

            def read(self, _size):
                return b""

        class Connection:
            def __init__(self, _host, _port, **kwargs):
                timeouts.append(kwargs["timeout"])
                self.sock = self

            def connect(self):
                pass

            def settimeout(self, value):
                timeouts.append(value)

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        client = HTTPClient("synthetic", max_retries=0)
        client.budget_deadline = time.monotonic() + 0.1
        with patch.dict(os.environ, {"LITERATURE_RADAR_OFFLINE_TEST": ""}), \
             patch("literature_radar.sources.http.http.client.HTTPSConnection", Connection):
            self.assertEqual(client.get("https://example.invalid").status, 200)
        self.assertTrue(timeouts)
        self.assertLessEqual(max(timeouts), 0.1)
