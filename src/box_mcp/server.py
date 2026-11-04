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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._arguments import EnvelopeFastMCP
from .api_client import BoxClient
from .auth import HEADER_CLIENT_ID, HEADER_CLIENT_SECRET, HEADER_ENTERPRISE_ID, REQUIRED_HEADERS, BoxCredentials
from .config import Settings

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[BoxCredentials | None] = contextvars.ContextVar(
    "box_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("box_request_id", default="-")

INSTRUCTIONS = (
    "Box is cloud content/file storage: folders and files under a Box enterprise, "
    "reachable here with one Service Account (Client ID / Secret / Enterprise ID).\n"
    "Usage:\n"
    "- Browse with box_get_folder_info / box_list_folder_items, starting from folder "
    "id '0' (the root) when the user does not give one.\n"
    "- Read a file with box_get_file_info; box_get_file_download_url returns a "
    "time-limited URL, never the file's bytes.\n"
    "- box_search finds files/folders by keyword across the whole enterprise.\n"
    "- Create/update/upload tools change content: call them only when the user "
    "explicitly asks, with the exact values given. Delete tools are permanent and "
    "also need confirm: true (and recursive: true for a non-empty folder) after the "
    "user approved that exact deletion.\n"
    "- box_create_shared_link / box_get_shared_link manage a link's access level "
    "(open / company / collaborators) for one file or folder."
)


def get_client_from_context() -> BoxClient | None:
    """A client built from the current request's credentials, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return BoxClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads the three X-Box-* headers on /mcp requests; 401 lists whichever are missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        client_id = _clean(request.headers.get(HEADER_CLIENT_ID))
        client_secret = _clean(request.headers.get(HEADER_CLIENT_SECRET))
        enterprise_id = _clean(request.headers.get(HEADER_ENTERPRISE_ID))
        values = {HEADER_CLIENT_ID: client_id, HEADER_CLIENT_SECRET: client_secret, HEADER_ENTERPRISE_ID: enterprise_id}
        missing = [h for h in REQUIRED_HEADERS if not values[h]]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(BoxCredentials(client_id, client_secret, enterprise_id))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="box-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], BoxClient | None] = get_client_from_context

    from .tools import connection, files, folders, search, sharing

    for module in (connection, folders, files, search, sharing):
        module.register(mcp, client_factory)

    return mcp
