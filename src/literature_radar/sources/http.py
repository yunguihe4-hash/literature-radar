"""Small standard-library HTTPS client with per-provider limits and safe errors."""
from __future__ import annotations

import email.utils
import http.client
import importlib.metadata
import json
import os
import random
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import proxy_bypass_environment


def _default_user_agent(contact_email: str | None) -> str:
    try:
        version = importlib.metadata.version("literature-radar-core")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    agent = f"literature-radar-core/{version}"
    if contact_email:
        if ("@" not in contact_email or any(char.isspace() for char in contact_email)
                or any(char in contact_email for char in "()<>")):
            raise ValueError("invalid HTTP contact email")
        agent += f" (mailto:{contact_email})"
    return agent


class RequestFailure(RuntimeError):
    def __init__(self, provider_id: str, category: str, status: int | None = None):
        self.provider_id, self.category, self.status = provider_id, category, status
        super().__init__(f"{provider_id}: {category}" + (f" (HTTP {status})" if status else ""))


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float = 0.0


class HTTPClient:
    def __init__(self, provider_id: str, *, rate_per_second: float = 1.0,
                 connect_timeout: float = 5.0, read_timeout: float = 15.0,
                 total_timeout: float = 25.0, max_retries: int = 2,
                 max_retry_delay: float = 60.0,
                 user_agent: str | None = None, contact_email: str | None = None,
                 proxy_url: str | None = None, use_environment_proxy: bool = False,
                 transport: Callable[[str, Mapping[str, str]], HTTPResponse] | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        if rate_per_second <= 0 or min(connect_timeout, read_timeout, total_timeout, max_retry_delay) <= 0 or max_retries < 0:
            raise ValueError("invalid HTTP limit or timeout")
        if type(use_environment_proxy) is not bool:
            raise ValueError("use_environment_proxy must be boolean")
        if proxy_url is not None:
            self._parse_proxy(proxy_url)
        if user_agent is not None and (not user_agent or "\r" in user_agent or "\n" in user_agent):
            raise ValueError("invalid HTTP user agent")
        self.provider_id = provider_id
        self.interval = 1.0 / rate_per_second
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.total_timeout, self.max_retries = total_timeout, max_retries
        self.max_retry_delay = max_retry_delay
        self.user_agent = user_agent or _default_user_agent(contact_email)
        self.proxy_url, self.use_environment_proxy = proxy_url, use_environment_proxy
        self.transport, self.sleeper = transport, sleeper
        self._last_request = 0.0
        # Discovery sets this for the current semantic query group. Retries,
        # rate waits and a single HTTPS request must share the same budget.
        self.budget_deadline: float | None = None
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None

    def _remaining_budget(self) -> float:
        return (float("inf") if self.budget_deadline is None else
                self.budget_deadline - time.monotonic())

    @staticmethod
    def _parse_proxy(proxy_url: str) -> tuple[str, int]:
        parsed = urlsplit(proxy_url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("proxy_url must be an unauthenticated HTTP proxy URL")
        return parsed.hostname, parsed.port or 80

    def _proxy_for(self, hostname: str) -> tuple[str, int] | None:
        if not (self.proxy_url or self.use_environment_proxy):
            return None
        bypass = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
        if bypass and proxy_bypass_environment(hostname, {"no": bypass}):
            return None
        proxy = self.proxy_url
        if proxy is None:
            proxy = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                     or os.getenv("HTTP_PROXY") or os.getenv("http_proxy"))
        return self._parse_proxy(proxy) if proxy else None

    def _network(self, url: str, headers: Mapping[str, str]) -> HTTPResponse:
        if os.getenv("LITERATURE_RADAR_OFFLINE_TEST") == "1":
            raise RequestFailure(self.provider_id, "offline_test_mode")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RequestFailure(self.provider_id, "invalid_endpoint")
        remaining_budget = self._remaining_budget()
        if remaining_budget <= 0:
            raise RequestFailure(self.provider_id, "budget_exhausted")
        start = time.monotonic()
        request_timeout = min(self.total_timeout, remaining_budget)
        proxy = self._proxy_for(parsed.hostname)
        connection = http.client.HTTPSConnection(
            proxy[0] if proxy else parsed.hostname,
            proxy[1] if proxy else parsed.port,
            timeout=min(self.connect_timeout, request_timeout),
            context=ssl.create_default_context())
        try:
            if proxy:
                connection.set_tunnel(parsed.hostname, parsed.port or 443)
            connection.connect()
            remaining = request_timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError()
            connection.sock.settimeout(min(self.read_timeout, remaining))
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers=dict(headers))
            remaining = request_timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError()
            connection.sock.settimeout(min(self.read_timeout, remaining))
            response = connection.getresponse()
            chunks = []
            size = 0
            while True:
                remaining = request_timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise TimeoutError()
                # A server may send Connection: close; http.client then clears
                # connection.sock while the response stream is still readable.
                if connection.sock is not None:
                    connection.sock.settimeout(min(self.read_timeout, remaining))
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 8_000_000:
                    raise RequestFailure(self.provider_id, "response_too_large")
                chunks.append(chunk)
            return HTTPResponse(response.status,
                                {key.lower(): value for key, value in response.getheaders()},
                                b"".join(chunks), time.monotonic() - start)
        finally:
            connection.close()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return None

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None) -> HTTPResponse:
        if not endpoint.startswith("https://"):
            raise RequestFailure(self.provider_id, "invalid_endpoint")
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = endpoint + (("&" if "?" in endpoint else "?") + query if query else "")
        request_headers = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        for attempt in range(self.max_retries + 1):
            if self._remaining_budget() <= 0:
                raise RequestFailure(self.provider_id, "budget_exhausted")
            network_error = None
            wait = self.interval - (time.monotonic() - self._last_request)
            if wait > 0:
                if wait >= self._remaining_budget():
                    raise RequestFailure(self.provider_id, "budget_exhausted")
                self.sleeper(wait)
            self._last_request = time.monotonic()
            try:
                response = (self.transport or self._network)(url, request_headers)
            except (TimeoutError, socket.timeout) as exc:
                category = "timeout"
                response = None
                network_error = exc
            except (ConnectionError, OSError, ssl.SSLError) as exc:
                category = "connection"
                response = None
                network_error = exc
            if response is not None:
                self.last_status = response.status
                self.last_headers = {k.lower(): v for k, v in response.headers.items()}
                if self.provider_id == "crossref":
                    try:
                        limit = float(self.last_headers["x-rate-limit-limit"])
                        interval_text = self.last_headers["x-rate-limit-interval"].strip().lower()
                        unit = interval_text[-1]
                        seconds = float(interval_text[:-1]) * {"s": 1, "m": 60, "h": 3600}[unit]
                        if limit > 0 and seconds > 0:
                            self.interval = max(self.interval, seconds / limit)
                    except (KeyError, IndexError, ValueError, TypeError, ZeroDivisionError):
                        pass
                if 200 <= response.status < 300:
                    return response
                if response.status in (401, 403):
                    raise RequestFailure(self.provider_id, "authentication_or_permission", response.status)
                if response.status == 400:
                    raise RequestFailure(self.provider_id, "invalid_query_or_cursor", response.status)
                category = "rate_limited" if response.status == 429 else "server_error" if response.status in (500, 502, 503, 504) else "http_error"
                if category == "http_error":
                    raise RequestFailure(self.provider_id, category, response.status)
            if attempt == self.max_retries:
                raise RequestFailure(self.provider_id, category,
                                     response.status if response else None) from network_error
            retry_after = self._retry_after(self.last_headers.get("retry-after")) if response else None
            if retry_after is not None and retry_after > self.max_retry_delay:
                raise RequestFailure(self.provider_id, "retry_later", response.status)
            delay = retry_after if retry_after is not None else min(
                self.max_retry_delay, 2 ** attempt + random.random())
            if delay >= self._remaining_budget():
                raise RequestFailure(self.provider_id, "budget_exhausted")
            self.sleeper(delay)
        raise AssertionError("unreachable")

    def get_json(self, endpoint: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> Any:
        response = self.get(endpoint, params, headers)
        try:
            return json.loads(response.body)
        except (ValueError, UnicodeError) as exc:
            raise RequestFailure(self.provider_id, "invalid_json", response.status) from exc
