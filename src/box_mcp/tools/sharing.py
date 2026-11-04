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
"""Shared links for a file or folder."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import BoxClient
from ._common import READ_ONLY, WRITES_IDEMPOTENT, ClientFactory, compact, run_tool

ItemType = Annotated[Literal["file", "folder"], Field(description="Whether id is a file or a folder.")]
ItemId = Annotated[str, Field(description="Box file or folder id, matching item_type.")]


def _path(item_type: str, id: str) -> str:
    return f"{item_type}s/{id}"


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def box_create_shared_link(
        item_type: ItemType,
        id: ItemId,
        access: Annotated[Literal["open", "company", "collaborators"], Field(description="Who the link allows access to.")],
    ) -> str:
        """Create (or replace) a shared link for a file or folder. Only when the user explicitly asks."""

        async def action(client: BoxClient) -> dict:
            updated = await client.put(_path(item_type, id), {"shared_link": {"access": access}})
            link = updated.get("shared_link") or {}
            return compact({"id": id, "url": link.get("url"), "access": link.get("access")})

        return await run_tool("box_create_shared_link", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def box_get_shared_link(item_type: ItemType, id: ItemId) -> str:
        """Get a file or folder's existing shared link, or null if it has none."""

        async def action(client: BoxClient) -> dict:
            item = await client.get(_path(item_type, id), {"fields": "shared_link"})
            link = item.get("shared_link")
            if not link:
                return {"shared_link": None}
            return {"shared_link": compact({"url": link.get("url"), "access": link.get("access")})}

        return await run_tool("box_get_shared_link", client_factory, action)
