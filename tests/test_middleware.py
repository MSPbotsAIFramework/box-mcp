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
"""Credential middleware: 401 on missing headers, isolation across concurrent requests (AC2, AC17)."""

import asyncio

import pytest
from starlette.testclient import TestClient

from box_mcp.__main__ import build_http_app
from box_mcp.server import GatewayCredentialsMiddleware, _credentials_var, create_mcp_server, get_client_from_context

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {
    "X-Box-Client-Id": "client-id",
    "X-Box-Client-Secret": "client-secret",
    "X-Box-Enterprise-Id": "enterprise-id",
}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "missing_header",
    ["X-Box-Client-Id", "X-Box-Client-Secret", "X-Box-Enterprise-Id"],
)
def test_missing_any_header_returns_401_naming_it(missing_header):
    headers = {k: v for k, v in CREDS.items() if k != missing_header}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert missing_header in resp.json()["message"]
    assert resp.json()["required_headers"] == list(CREDS)


def test_no_headers_names_all_three():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS)
    assert resp.status_code == 401
    for header in CREDS:
        assert header in resp.json()["message"]


def test_placeholder_value_counts_as_missing():
    with _client() as client:
        resp = client.post(
            "/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | {"X-Box-Client-Secret": "${BOX_CLIENT_SECRET}"}
        )
    assert resp.status_code == 401
    assert "X-Box-Client-Secret" in resp.json()["message"]


def test_no_token_exchange_when_headers_are_missing(monkeypatch):
    called = []

    async def fail_if_called(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("token endpoint must not be called when credentials are missing")

    import box_mcp.auth as auth_module

    monkeypatch.setattr(auth_module, "exchange_token", fail_if_called)
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | {"X-Box-Client-Id": "x"})
    assert resp.status_code == 401
    assert not called


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("box_test_connection", "box_get_folder_info", "box_delete_file"):
        assert name in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def _headers(client_id: str, client_secret: str, enterprise_id: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-box-client-id", client_id.encode()),
        (b"x-box-client-secret", client_secret.encode()),
        (b"x-box-enterprise-id", enterprise_id.encode()),
    ]


def test_credentials_reach_request_context_and_are_reset():
    seen = _run_middleware(_headers("cid", "csecret", "eid"))
    client = seen["client"]
    assert client._credentials.client_id == "cid"
    assert client._credentials.enterprise_id == "eid"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    """AC17: two concurrent requests with different Enterprise IDs never see each other's creds."""
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().enterprise_id
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().enterprise_id))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(enterprise_id: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": _headers("cid", "csecret", enterprise_id.decode())}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_concurrent_tool_calls_never_cross_tenant_tokens(monkeypatch):
    """AC17 end-to-end: each request's Box API call uses only that request's own exchanged token (TC23)."""
    import box_mcp.api_client as api_client_module

    async def fake_exchange(credentials, _http_client):
        await asyncio.sleep(0.01)  # force interleaving across the two concurrent calls
        return f"token-for-{credentials.enterprise_id}"

    # api_client.py imported `exchange_token` by name, so the call site resolves
    # through its own module attribute, not auth's -- patch it there.
    monkeypatch.setattr(api_client_module, "exchange_token", fake_exchange)

    async def fake_get(self, path, params=None):
        # Surfaces which exchanged token this client instance actually holds.
        return {"id": "u1", "name": "n", "login": await self._access_token()}

    monkeypatch.setattr(api_client_module.BoxClient, "get", fake_get)

    async def call_for(enterprise_id: str) -> str:
        token = _credentials_var.set(api_client_module.BoxCredentials("cid", "secret", enterprise_id))
        try:
            content, _ = await create_mcp_server().call_tool("box_test_connection", {})
        finally:
            _credentials_var.reset(token)
        return content[0].text

    async def both():
        return await asyncio.gather(call_for("tenant-a"), call_for("tenant-b"))

    result_a, result_b = asyncio.run(both())
    assert "token-for-tenant-a" in result_a and "token-for-tenant-b" not in result_a
    assert "token-for-tenant-b" in result_b and "token-for-tenant-a" not in result_b


def test_credentials_repr_hides_secret():
    seen = _run_middleware(_headers("cid", "very-secret-value", "eid"))
    assert "very-secret-value" not in repr(seen["credentials"])
