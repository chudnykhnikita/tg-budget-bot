import unittest
from unittest.mock import patch

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest as RealHTTPXRequest

from proxy_failover import ProxyFailoverRequest


def _transport_error(error_type, message):
    error = NetworkError(message)
    error.__cause__ = error_type(message)
    return error


class _FakeRequest:
    def __init__(self, proxy=None, **_options):
        self.proxy = proxy
        self.read_timeout = 5
        self.outcomes = []
        self.calls = []

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, **_kwargs):
        self.calls.append((url, method))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ProxyFailoverRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.httpx_request_patch = patch("proxy_failover.HTTPXRequest", _FakeRequest)
        self.httpx_request_patch.start()
        self.addCleanup(self.httpx_request_patch.stop)
        self.request = ProxyFailoverRequest(
            [
                ("PROXY_URL", "http://primary.invalid:8080"),
                ("PROXY_URL_BACKUP", "http://backup.invalid:8080"),
                ("direct", None),
            ],
            probe_interval=3600,
        )
        self.routes = self.request._requests

    async def test_connect_failure_retries_same_request_on_next_proxy(self):
        self.routes[0].outcomes.append(
            _transport_error(httpx.ConnectError, "primary unavailable")
        )
        self.routes[1].outcomes.append((200, b"ok"))

        result = await self.request.do_request("https://api.telegram.org/bot/x", "POST")

        self.assertEqual(result, (200, b"ok"))
        self.assertEqual(len(self.routes[0].calls), 1)
        self.assertEqual(len(self.routes[1].calls), 1)
        self.assertEqual(self.request._active_route, 1)

    async def test_http_conflict_response_does_not_rotate_route(self):
        self.routes[0].outcomes.append((409, b"conflict"))

        result = await self.request.do_request("https://api.telegram.org/bot/x", "POST")

        self.assertEqual(result, (409, b"conflict"))
        self.assertEqual(self.routes[1].calls, [])
        self.assertEqual(self.request._active_route, 0)

    async def test_ambiguous_write_failure_switches_route_without_replay(self):
        self.routes[0].outcomes.append(
            _transport_error(httpx.ReadError, "response connection lost")
        )

        with self.assertRaisesRegex(NetworkError, "outcome is unknown"):
            await self.request.do_request("https://api.telegram.org/bot/x/sendMessage", "POST")

        self.assertEqual(len(self.routes[0].calls), 1)
        self.assertEqual(self.routes[1].calls, [])
        self.assertEqual(self.request._active_route, 1)

    async def test_polling_read_timeout_retries_on_next_route(self):
        timeout = TimedOut("poll read timeout")
        timeout.__cause__ = httpx.ReadTimeout("poll read timeout")
        self.routes[0].outcomes.append(timeout)
        self.routes[1].outcomes.append((200, b"updates"))

        result = await self.request.do_request(
            "https://api.telegram.org/bot/x/getUpdates", "POST"
        )

        self.assertEqual(result, (200, b"updates"))
        self.assertEqual(self.request._active_route, 1)

    async def test_recovered_preferred_proxy_is_used_after_probe(self):
        self.request._active_route = 1
        self.request._next_probe_at = 0
        self.routes[0].outcomes.extend([(404, b"not found"), (200, b"ok")])

        result = await self.request.do_request("https://api.telegram.org/bot/x/sendMessage", "POST")

        self.assertEqual(result, (200, b"ok"))
        self.assertEqual(self.routes[0].calls[0], ("https://api.telegram.org", "GET"))
        self.assertEqual(self.request._active_route, 0)

    async def test_all_routes_failed_returns_generic_network_error(self):
        for route in self.routes:
            route.outcomes.append(_transport_error(httpx.ConnectError, "unavailable"))

        with self.assertRaisesRegex(NetworkError, "All Telegram connection routes failed") as raised:
            await self.request.do_request("https://api.telegram.org/bot/x/getUpdates", "POST")

        self.assertNotIn("primary.invalid", str(raised.exception))
        self.assertEqual(self.request._active_route, 0)

    async def test_invalid_proxy_configuration_is_skipped(self):
        with patch("proxy_failover.HTTPXRequest", RealHTTPXRequest):
            request = ProxyFailoverRequest(
                [("bad-proxy", "ftp://proxy.invalid"), ("direct", None)]
            )
        self.assertEqual(request._routes, (("direct", None),))
        await request.initialize()
        await request.shutdown()


if __name__ == "__main__":
    unittest.main()
