# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the Box REST API v2.0.

One ``BoxClient`` is created per incoming MCP request (see ``server.py``), built from
that request's own credentials. The exchanged access token lives only on the
instance (``self._token``), fetched lazily on first use and reused for the rest of
that one request's Box calls; it is never cached at module level or across requests
(see ``auth.py``).
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from .auth import BoxCredentials, exchange_token
from .config import API_BASE_URL, UPLOAD_BASE_URL
from .errors import BoxError

logger = logging.getLogger("box_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    return CookieJar(policy=_RejectAllCookies())


# One connection pool for the process. It never holds credentials: the
# Authorization header is attached per instance from that request's own
# exchanged token, so sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


class BoxClient:
    """Thin wrapper over the Box API for one caller's Service Account credentials."""

    def __init__(self, credentials: BoxCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client or _get_shared_http_client()
        self._token: str | None = None

    async def _access_token(self) -> str:
        if self._token is None:
            self._token = await exchange_token(self._credentials, self._http)
        return self._token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}", "Accept": "application/json"}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request("GET", path, params=params))[0]

    async def post(self, path: str, body: Any = None) -> Any:
        return (await self._request("POST", path, body=body))[0]

    async def put(self, path: str, body: Any = None) -> Any:
        return (await self._request("PUT", path, body=body))[0]

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request("DELETE", path, params=params))[0]

    async def redirect_location(self, path: str) -> str:
        """GET a path that Box answers with a 302 and a Location header, without following it.

        Used for file download URLs (AC9): the file's bytes must never be embedded in
        the tool's own JSON response, only the time-limited URL Box hands back.
        """
        headers = await self._auth_headers()
        url = f"{API_BASE_URL}/{path.lstrip('/')}"

        async def send() -> httpx.Response:
            return await self._http.get(url, headers=headers, follow_redirects=False)

        resp = await self._send_with_retry("GET", path, send)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if location:
                return location
            raise BoxError(resp.status_code, f"Box returned a redirect for {path} without a Location header")
        if resp.status_code >= 400:
            raise BoxError(resp.status_code, _error_message(resp, path))
        raise BoxError(resp.status_code, f"Box returned HTTP {resp.status_code} for {path}, expected a redirect")

    async def upload(self, filename: str, parent_id: str, content: bytes) -> Any:
        """POST a new file's content to the Box upload host (multipart)."""
        headers = await self._auth_headers()
        attributes = {"name": filename, "parent": {"id": parent_id}}
        files = {
            "attributes": (None, json.dumps(attributes), "application/json"),
            "file": (filename, content, "application/octet-stream"),
        }

        async def send() -> httpx.Response:
            return await self._http.post(f"{UPLOAD_BASE_URL}/files/content", headers=headers, files=files)

        resp = await self._send_with_retry("POST", "upload/files/content", send)
        if resp.status_code >= 400:
            raise BoxError(resp.status_code, _error_message(resp, "files/content"))
        payload = resp.json()
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return entries[0] if entries else payload

    async def _send_with_retry(
        self, method: str, log_path: str, send: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        """Run one Box call via `send`, retrying with backoff on the same policy for every caller.

        429 is refused before processing, so any method may retry it; other failures
        (timeout, network error, 5xx) are retried only for reads, so a write never runs
        twice. Shared by `_request`, `redirect_location` and `upload` so a tool's
        transport shape (JSON body vs. no-redirect GET vs. multipart POST) never changes
        its retry/backoff behavior -- see README.md's "Reads are retried with backoff on
        429, 5xx and network errors; writes only on 429."
        """
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: BoxError | None = None
            try:
                resp = await send()
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, BoxError(0, f"Timed out calling Box ({method} {log_path})")
            except httpx.RequestError as e:
                status, error = 0, BoxError(0, f"Network error calling Box ({method} {log_path}): {type(e).__name__}")

            logger.info(
                "box_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, log_path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            return resp

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> tuple[Any, httpx.Response]:
        url = f"{API_BASE_URL}/{path.lstrip('/')}"
        headers = await self._auth_headers()
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}

        async def send() -> httpx.Response:
            return await self._http.request(method, url, params=query, json=body, headers=headers)

        resp = await self._send_with_retry(method, path, send)
        if resp.status_code >= 400:
            raise BoxError(resp.status_code, _error_message(resp, path))
        if not resp.content or not resp.content.strip():
            return None, resp
        try:
            return resp.json(), resp
        except ValueError:
            return {"raw_response": resp.text[:2000]}, resp


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _error_message(resp: httpx.Response, path: str) -> str:
    # Box's error body ({"status","code","message","context_info",...}) never contains
    # the caller's client_secret, so it is safe to surface (truncated) as-is.
    fallback = f"Box returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        if text.lower().startswith("<"):
            return fallback  # an HTML error page, not useful to the agent
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error_description") or payload.get("error")
        if message:
            return f"{fallback}: {str(message)[:500]}"
    return fallback
