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
"""Folders: browse, create, rename/move, delete."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import BoxClient
from ._common import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    Confirm,
    FolderId,
    Limit,
    Offset,
    OptionalFolderId,
    Recursive,
    compact,
    list_page,
    not_confirmed,
    require,
    run_tool,
)

_FOLDER_FIELDS = "id,name,parent,size,owned_by,item_collection"


def _folder_info(folder: dict) -> dict:
    parent = folder.get("parent") or {}
    owner = folder.get("owned_by") or {}
    items = folder.get("item_collection") or {}
    return compact(
        {
            "id": folder.get("id"),
            "name": folder.get("name"),
            "parent_id": parent.get("id"),
            "size": folder.get("size"),
            "item_count": items.get("total_count"),
            "owner_login": owner.get("login"),
        }
    )


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def box_get_folder_info(id: FolderId) -> str:
        """Get a folder's id, name, parent folder id, size, item count and owner login."""

        async def action(client: BoxClient) -> dict:
            return _folder_info(await client.get(f"folders/{id}", {"fields": _FOLDER_FIELDS}))

        return await run_tool("box_get_folder_info", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def box_list_folder_items(id: FolderId, limit: Limit = None, offset: Offset = None) -> str:
        """List a folder's direct items (files and subfolders): id, name, type. Paginated."""

        async def action(client: BoxClient) -> dict:
            page = await list_page(
                client, f"folders/{id}/items", "entries", {"fields": "id,name,type"}, limit, offset
            )
            page["entries"] = [compact({"id": e.get("id"), "name": e.get("name"), "type": e.get("type")}) for e in page["entries"]]
            return page

        return await run_tool("box_list_folder_items", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def box_create_folder(parent_id: FolderId, name: str) -> str:
        """Create a new folder under parent_id. Only when the user explicitly asks."""

        async def action(client: BoxClient) -> Any:
            created = await client.post("folders", {"name": name, "parent": {"id": parent_id}})
            return compact({"id": created.get("id"), "name": created.get("name")})

        return await run_tool("box_create_folder", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def box_update_folder(id: FolderId, name: str | None = None, parent_id: OptionalFolderId = None) -> str:
        """Rename and/or move a folder; only the fields given change. Only when the user explicitly asks."""

        async def action(client: BoxClient) -> Any:
            body = require(compact({"name": name, "parent": compact({"id": parent_id}) or None}))
            updated = await client.put(f"folders/{id}", body)
            parent = updated.get("parent") or {}
            return compact({"id": updated.get("id"), "name": updated.get("name"), "parent_id": parent.get("id")})

        return await run_tool("box_update_folder", client_factory, action)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def box_delete_folder(id: FolderId, confirm: Confirm = None, recursive: Recursive = None) -> str:
        """Permanently delete a folder. Irreversible; needs confirm: true.

        Non-empty folders also need recursive: true. Only when the user explicitly asks.
        """

        async def action(client: BoxClient) -> Any:
            if confirm is not True:
                return not_confirmed(
                    f"Permanently delete folder {id}", extra="(add recursive: true too if it still has items)"
                )
            await client.delete(f"folders/{id}", {"recursive": bool(recursive)})
            return {"done": True, "deleted_folder_id": id}

        return await run_tool("box_delete_folder", client_factory, action)
