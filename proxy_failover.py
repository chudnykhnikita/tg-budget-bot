"""Retry Telegram requests through configured proxy routes when transport fails."""

import asyncio
import logging
import time
from typing import Any, Optional, Sequence

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import BaseRequest, HTTPXRequest, RequestData

logger = logging.getLogger(__name__)

Route = tuple[str, Optional[str]]
_TELEGRAM_ROOT = "https://api.telegram.org"


class ProxyFailoverRequest(BaseRequest):
    """A PTB request that rotates routes on transport errors.

    Routes are tried in order, with direct connectivity represented by a
    final ``(label, None)`` entry. A healthy route is retained for subsequent
    requests. If a fallback is active, proxy routes are periodically probed
    so a recovered route can become active without restarting the bot.
    """

    def __init__(
        self,
        routes: Sequence[Route],
        *,
        initial_route: int = 0,
        probe_interval: float = 60.0,
        **request_options: Any,
    ) -> None:
        if not routes:
            raise ValueError("At least one Telegram connection route is required")
        if not 0 <= initial_route < len(routes):
            raise ValueError("initial_route is outside the configured routes")

        valid_routes: list[Route] = []
        requests: list[HTTPXRequest] = []
        source_to_valid: dict[int, int] = {}
        for source_index, (label, proxy_url) in enumerate(routes):
            try:
                request = HTTPXRequest(proxy=proxy_url, **request_options)
            except (
                TypeError,
                ValueError,
                ImportError,
                RuntimeError,
                httpx.InvalidURL,
            ) as error:
                logger.warning(
                    "Telegram route %s has invalid configuration (%s)",
                    label,
                    type(error).__name__,
                )
                continue
            source_to_valid[source_index] = len(valid_routes)
            valid_routes.append((label, proxy_url))
            requests.append(request)

        if not valid_routes:
            raise ValueError("No usable Telegram connection route is configured")

        self._routes = tuple(valid_routes)
        self._requests = tuple(requests)
        proxy_indices = [
            index for index, (_, proxy_url) in enumerate(self._routes) if proxy_url is not None
        ]
        self._active_route = source_to_valid.get(
            initial_route,
            proxy_indices[0] if proxy_indices else 0,
        )
        self._preferred_route = source_to_valid.get(
            0,
            proxy_indices[0] if proxy_indices else 0,
        )
        self._state_generation = 0
        self._probe_interval = probe_interval
        self._next_probe_at = time.monotonic() + probe_interval
        self._state_lock = asyncio.Lock()

    @property
    def read_timeout(self) -> Optional[float]:
        return self._requests[self._active_route].read_timeout

    async def initialize(self) -> None:
        initialized: list[HTTPXRequest] = []
        try:
            for request in self._requests:
                await request.initialize()
                initialized.append(request)
        except Exception:
            await asyncio.gather(
                *(request.shutdown() for request in initialized),
                return_exceptions=True,
            )
            raise

    async def shutdown(self) -> None:
        results = await asyncio.gather(
            *(request.shutdown() for request in self._requests),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _probe_preferred_routes(self) -> None:
        now = time.monotonic()
        if now < self._next_probe_at:
            return
        self._next_probe_at = now + self._probe_interval

        # The direct route is deliberately excluded: the configured proxies
        # are preferred whenever one becomes reachable again.
        for route_index, (label, proxy_url) in enumerate(self._routes):
            if proxy_url is None or route_index == self._active_route:
                continue
            try:
                status, _ = await self._requests[route_index].do_request(
                    _TELEGRAM_ROOT,
                    "GET",
                    connect_timeout=3.0,
                    read_timeout=3.0,
                    write_timeout=3.0,
                    pool_timeout=3.0,
                )
            except (NetworkError, TimedOut) as error:
                logger.debug(
                    "Telegram route probe failed for %s (%s)",
                    label,
                    type(error).__name__,
                )
                continue

            # The API root may return 404; any non-server-error response
            # confirms that this route reaches Telegram's API host.
            if status < 500:
                previous_label = self._routes[self._active_route][0]
                self._active_route = route_index
                self._state_generation += 1
                logger.info(
                    "Telegram route recovered: %s -> %s",
                    previous_label,
                    label,
                )
                return

    def _attempt_order(self, active: int) -> list[int]:
        proxy_indices = [
            index
            for index, (_, proxy_url) in enumerate(self._routes)
            if proxy_url is not None and index != active
        ]
        direct_indices = [
            index
            for index, (_, proxy_url) in enumerate(self._routes)
            if proxy_url is None and index != active
        ]
        return [active, *proxy_indices, *direct_indices]

    @staticmethod
    def _safe_to_retry(error: BaseException, url: str) -> bool:
        """Only replay requests known not to have reached Telegram.

        Polling is safe to retry with the same offset. For other methods,
        retry only connection-stage failures; a read/write failure after a
        request was sent could otherwise duplicate a successful Bot API call.
        """
        if url.rsplit("/", 1)[-1] == "getUpdates":
            return True

        cause: Optional[BaseException] = error
        while cause is not None:
            if isinstance(
                cause,
                (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                    httpx.ProxyError,
                    httpx.PoolTimeout,
                ),
            ):
                return True
            cause = cause.__cause__
        return False

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: Optional[RequestData] = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        async with self._state_lock:
            if self._active_route != self._preferred_route:
                await self._probe_preferred_routes()
            start_route = self._active_route
            start_generation = self._state_generation
        last_error: Optional[BaseException] = None
        for route_index in self._attempt_order(start_route):
            try:
                result = await self._requests[route_index].do_request(
                    url=url,
                    method=method,
                    request_data=request_data,
                    read_timeout=read_timeout,
                    write_timeout=write_timeout,
                    connect_timeout=connect_timeout,
                    pool_timeout=pool_timeout,
                )
            except (NetworkError, TimedOut) as error:
                last_error = error
                label = self._routes[route_index][0]
                logger.warning(
                    "Telegram route %s failed (%s)",
                    label,
                    type(error).__name__,
                )
                if not self._safe_to_retry(error, url):
                    order = self._attempt_order(start_route)
                    next_position = order.index(route_index) + 1
                    next_route = (
                        order[next_position]
                        if next_position < len(order)
                        else self._preferred_route
                    )
                    async with self._state_lock:
                        if self._state_generation == start_generation:
                            self._active_route = next_route
                            self._state_generation += 1
                    raise NetworkError(
                        "Telegram request outcome is unknown; switched route for the next request"
                    ) from error
                continue

            async with self._state_lock:
                self._active_route = route_index
                self._state_generation += 1
            if route_index != start_route:
                logger.warning(
                    "Telegram route failover: %s -> %s",
                    self._routes[start_route][0],
                    self._routes[route_index][0],
                )
            return result

        async with self._state_lock:
            # Start the next polling retry from the preferred route, which may
            # have recovered since this full round of attempts.
            if self._state_generation == start_generation:
                self._active_route = self._preferred_route
                self._state_generation += 1
        raise NetworkError("All Telegram connection routes failed") from last_error
