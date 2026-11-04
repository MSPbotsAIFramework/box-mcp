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
"""Request shapes, retries and tool behaviour against a mocked Box API. No network access.

Covers the `auto` cases in docs/tickets/PRD-21298.ac.json (TC1, TC3-TC9, TC11-TC16,
TC18-TC23). The `manual` cases (TC2, TC10, TC17) need a real Box Service Account and
are not executed here (see docs/tickets/PRD-21298.ac.json).
"""

import base64
import json

import httpx
import pytest

from box_mcp import api_client
from box_mcp.api_client import BoxClient, BoxCredentials
from box_mcp.server import _credentials_var, create_mcp_server

CREDS = BoxCredentials("client-id", "s3cr3t-value", "enterprise-id")
TOKEN_RESPONSE = {"access_token": "test-token", "expires_in": 3599, "token_type": "bearer"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by method + path, separately for the token host and the API host."""

    def __init__(self, routes=None, default=None, token_response=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else {}
        self.token_response = token_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            reply = self.token_response if self.token_response is not None else TOKEN_RESPONSE
            if callable(reply):
                return reply(request)
            return httpx.Response(200, json=reply)
        prefix = "/api/2.0/" if request.url.host == "upload.box.com" else "/2.0/"
        path = request.url.path.removeprefix(prefix)
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if isinstance(reply, httpx.Response):
            return reply
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path) and (method is None or r.method == method)]

    def token_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/oauth2/token"]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> BoxClient:
    return BoxClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- connection / auth (TC1, TC3, TC22) -------------------------------------


async def test_connection_check_returns_authenticated_user(monkeypatch):
    rec = Recorder({"users/me": {"id": "1", "name": "Service Account", "login": "svc@enterprise.box.com"}})
    result = await _call(monkeypatch, rec, "box_test_connection", {})
    assert result == {"status": "ok", "user": {"id": "1", "name": "Service Account", "login": "svc@enterprise.box.com"}}
    assert len(rec.token_calls()) == 1  # exchanged once, reused for the one Box call in this tool


async def test_failed_token_exchange_is_unauthorized_not_invalid_argument(monkeypatch):
    # Box's token endpoint answers bad credentials with HTTP 400, not 401 -- the
    # connector must still report `unauthorized` (see auth.py:exchange_token).
    rec = Recorder(token_response=lambda r: httpx.Response(400, json={"error": "invalid_client", "error_description": "bad id/secret"}))
    result = await _call(monkeypatch, rec, "box_test_connection", {})
    assert result["error"]["code"] == "unauthorized"
    assert result["error"]["retryable"] is False
    assert CREDS.client_secret not in result["error"]["message"]
    assert not rec.calls("users/me")  # the API is never called without a token


async def test_error_message_never_echoes_client_secret(monkeypatch):
    rec = Recorder({"folders/404": lambda r: httpx.Response(404, json={"message": "Not Found"})})
    result = await _call(monkeypatch, rec, "box_get_folder_info", {"id": "404"})
    assert result["error"]["code"] == "not_found"
    assert CREDS.client_secret not in result["error"]["message"]
    assert CREDS.client_secret not in json.dumps(result)


# ---- folders (TC4-TC9) -------------------------------------------------------


async def test_get_folder_info(monkeypatch):
    rec = Recorder(
        {
            "folders/12345": {
                "id": "12345",
                "name": "Client Reports",
                "parent": {"id": "0"},
                "size": 48213991,
                "owned_by": {"login": "service-account@enterprise.box.com"},
                "item_collection": {"total_count": 37},
            }
        }
    )
    result = await _call(monkeypatch, rec, "box_get_folder_info", {"id": "12345"})
    assert result == {
        "id": "12345",
        "name": "Client Reports",
        "parent_id": "0",
        "size": 48213991,
        "item_count": 37,
        "owner_login": "service-account@enterprise.box.com",
    }


async def test_list_folder_items_default_paging(monkeypatch):
    entries = [{"id": str(i), "name": f"item-{i}", "type": "file"} for i in range(60)]

    def route(request):
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 25))
        return httpx.Response(200, json={"entries": entries[offset : offset + limit], "total_count": len(entries)})

    rec = Recorder({"folders/1/items": route})
    result = await _call(monkeypatch, rec, "box_list_folder_items", {"id": "1"})
    assert len(result["entries"]) == 25
    assert result["has_more"] is True
    assert result["next_offset"] == 25
    assert rec.requests[-1].url.params["limit"] == "25"


async def test_list_folder_items_limit_is_clamped_to_200(monkeypatch):
    rec = Recorder({"folders/1/items": {"entries": [], "total_count": 0}})
    await _call(monkeypatch, rec, "box_list_folder_items", {"id": "1", "limit": 500})
    assert rec.requests[-1].url.params["limit"] == "200"


async def test_create_folder(monkeypatch):
    rec = Recorder({"POST folders": {"id": "99", "name": "New Folder"}})
    result = await _call(monkeypatch, rec, "box_create_folder", {"parent_id": "0", "name": "New Folder"})
    assert body(rec.calls("folders", "POST")[0]) == {"name": "New Folder", "parent": {"id": "0"}}
    assert result == {"id": "99", "name": "New Folder"}


async def test_update_folder_name_only_body(monkeypatch):
    rec = Recorder({"PUT folders/5": {"id": "5", "name": "Renamed", "parent": {"id": "0"}}})
    result = await _call(monkeypatch, rec, "box_update_folder", {"id": "5", "name": "Renamed"})
    assert body(rec.calls("folders/5", "PUT")[0]) == {"name": "Renamed"}
    assert result == {"id": "5", "name": "Renamed", "parent_id": "0"}


async def test_delete_folder_without_confirm_makes_no_box_call(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "box_delete_folder", {"id": "7"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_delete_folder_with_confirm_and_recursive(monkeypatch):
    rec = Recorder({"DELETE folders/7": httpx.Response(204)})
    result = await _call(monkeypatch, rec, "box_delete_folder", {"id": "7", "confirm": True, "recursive": True})
    assert result == {"done": True, "deleted_folder_id": "7"}
    call = rec.calls("folders/7", "DELETE")[0]
    assert call.url.params["recursive"] == "true"


# ---- files (TC11-TC16) -------------------------------------------------------


async def test_get_file_info(monkeypatch):
    rec = Recorder(
        {"files/1": {"id": "1", "name": "report.pdf", "size": 2048, "parent": {"id": "9"}, "modified_at": "2026-01-01T00:00:00-08:00"}}
    )
    result = await _call(monkeypatch, rec, "box_get_file_info", {"id": "1"})
    assert result == {
        "id": "1",
        "name": "report.pdf",
        "size": 2048,
        "parent_id": "9",
        "modified_at": "2026-01-01T00:00:00-08:00",
    }


async def test_get_file_download_url_returns_location_not_bytes(monkeypatch):
    def route(request):
        return httpx.Response(302, headers={"Location": "https://dl.boxcloud.com/d/1/abc"})

    rec = Recorder({"files/1/content": route})
    result = await _call(monkeypatch, rec, "box_get_file_download_url", {"id": "1"})
    assert result == {"id": "1", "download_url": "https://dl.boxcloud.com/d/1/abc"}
    assert "download_url" in result and isinstance(result["download_url"], str)


async def test_upload_file_over_cap_is_rejected_without_a_request(monkeypatch):
    rec = Recorder()
    content = "x" * (11 * 1024 * 1024)
    result = await _call(monkeypatch, rec, "box_upload_file", {"parent_id": "0", "name": "big.txt", "content": content})
    assert result["error"]["code"] == "invalid_argument"
    assert "10 MB" in result["error"]["message"]
    assert not rec.calls("files/content")


async def test_upload_file_under_cap(monkeypatch):
    rec = Recorder({"POST files/content": {"entries": [{"id": "55", "name": "note.txt"}]}})
    result = await _call(monkeypatch, rec, "box_upload_file", {"parent_id": "0", "name": "note.txt", "content": "hello"})
    assert result == {"id": "55", "name": "note.txt"}
    sent = rec.calls("files/content", "POST")[0]
    assert b"note.txt" in sent.content and b"hello" in sent.content


async def test_upload_file_base64(monkeypatch):
    rec = Recorder({"POST files/content": {"entries": [{"id": "56", "name": "note.bin"}]}})
    payload = base64.b64encode(b"\x00\x01binary").decode()
    result = await _call(
        monkeypatch, rec, "box_upload_file", {"parent_id": "0", "name": "note.bin", "content": payload, "encoding": "base64"}
    )
    assert result == {"id": "56", "name": "note.bin"}
    assert b"\x00\x01binary" in rec.calls("files/content", "POST")[0].content


async def test_update_file_move_sends_parent_id(monkeypatch):
    rec = Recorder({"PUT files/1": {"id": "1", "name": "report.pdf", "parent": {"id": "42"}}})
    result = await _call(monkeypatch, rec, "box_update_file", {"id": "1", "parent_id": "42"})
    assert body(rec.calls("files/1", "PUT")[0]) == {"parent": {"id": "42"}}
    assert result["parent_id"] == "42"


async def test_delete_file_without_confirm_makes_no_box_call(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "box_delete_file", {"id": "3"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []
    result = await _call(monkeypatch, rec, "box_delete_file", {"id": "3", "confirm": True})
    assert result == {"done": True, "deleted_file_id": "3"}
    assert rec.calls("files/3", "DELETE")


# ---- search (TC18) -----------------------------------------------------------


async def test_search_returns_mocked_results(monkeypatch):
    rec = Recorder(
        {
            "search": {
                "entries": [
                    {"id": "1", "name": "a.txt", "type": "file"},
                    {"id": "2", "name": "b", "type": "folder"},
                    {"id": "3", "name": "c.txt", "type": "file"},
                ],
                "total_count": 3,
            }
        }
    )
    result = await _call(monkeypatch, rec, "box_search", {"query": "report"})
    assert [e["id"] for e in result["entries"]] == ["1", "2", "3"]
    assert rec.requests[-1].url.params["query"] == "report"


# ---- sharing (TC19, TC20) -----------------------------------------------------


async def test_create_shared_link(monkeypatch):
    rec = Recorder({"PUT files/1": {"shared_link": {"url": "https://box.com/s/abc", "access": "open"}}})
    result = await _call(monkeypatch, rec, "box_create_shared_link", {"item_type": "file", "id": "1", "access": "open"})
    assert body(rec.calls("files/1", "PUT")[0]) == {"shared_link": {"access": "open"}}
    assert result == {"id": "1", "url": "https://box.com/s/abc", "access": "open"}


async def test_get_shared_link_returns_null_when_none(monkeypatch):
    rec = Recorder({"folders/1": {"shared_link": None}})
    result = await _call(monkeypatch, rec, "box_get_shared_link", {"item_type": "folder", "id": "1"})
    assert result == {"shared_link": None}


async def test_get_shared_link_returns_existing_link(monkeypatch):
    rec = Recorder({"files/1": {"shared_link": {"url": "https://box.com/s/xyz", "access": "company"}}})
    result = await _call(monkeypatch, rec, "box_get_shared_link", {"item_type": "file", "id": "1"})
    assert result == {"shared_link": {"url": "https://box.com/s/xyz", "access": "company"}}


# ---- rate limiting / retries (TC21) ------------------------------------------


async def test_rate_limit_retries_then_succeeds(monkeypatch):
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json={"id": "1", "name": "x", "login": "y"}),
        ]
    )
    rec = Recorder({"users/me": lambda r: next(replies)})
    result = await _call(monkeypatch, rec, "box_test_connection", {})
    assert result == {"status": "ok", "user": {"id": "1", "name": "x", "login": "y"}}
    assert len(rec.calls("users/me")) == 2


async def test_rate_limit_exhausted_returns_rate_limited_error(monkeypatch):
    rec = Recorder({"GET users/me": lambda r: httpx.Response(429, headers={"Retry-After": "0"}, json={})})
    result = await _call(monkeypatch, rec, "box_test_connection", {})
    assert result["error"]["code"] == "rate_limited"
    assert result["error"]["retryable"] is True


async def test_not_found_never_raises(monkeypatch):
    rec = Recorder({"files/999": lambda r: httpx.Response(404, json={"message": "Not Found"})})
    result = await _call(monkeypatch, rec, "box_get_file_info", {"id": "999"})
    assert result == {"error": {"code": "not_found", "message": "Box returned HTTP 404 for files/999: Not Found", "retryable": False}}
