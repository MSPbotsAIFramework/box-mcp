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
"""Files: read metadata, get a download link, upload, rename/move, delete."""

import base64
import binascii
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import BoxClient
from ..config import MAX_UPLOAD_BYTES
from ._common import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    Confirm,
    FileId,
    FolderId,
    OptionalFolderId,
    compact,
    not_confirmed,
    require,
    run_tool,
)

_FILE_FIELDS = "id,name,size,parent,modified_at"


def _file_info(file: dict) -> dict:
    parent = file.get("parent") or {}
    return compact(
        {
            "id": file.get("id"),
            "name": file.get("name"),
            "size": file.get("size"),
            "parent_id": parent.get("id"),
            "modified_at": file.get("modified_at"),
        }
    )


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def box_get_file_info(id: FileId) -> str:
        """Get a file's id, name, size, parent folder id and last-modified timestamp."""

        async def action(client: BoxClient) -> dict:
            return _file_info(await client.get(f"files/{id}", {"fields": _FILE_FIELDS}))

        return await run_tool("box_get_file_info", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def box_get_file_download_url(id: FileId) -> str:
        """Get a time-limited direct download URL for a file's content. Never returns the file bytes."""

        async def action(client: BoxClient) -> dict:
            url = await client.redirect_location(f"files/{id}/content")
            return {"id": id, "download_url": url}

        return await run_tool("box_get_file_download_url", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def box_upload_file(
        parent_id: FolderId,
        name: str,
        content: Annotated[str, Field(description="File content, as plain text or base64 per `encoding`.")],
        encoding: Annotated[Literal["text", "base64"], Field(description="How `content` is encoded.")] = "text",
    ) -> str:
        """Upload a new file (max 10 MB) under parent_id. Only when the user explicitly asks."""

        async def action(client: BoxClient) -> Any:
            if encoding == "base64":
                try:
                    data = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError) as e:
                    raise ValueError(f"content is not valid base64: {e}") from None
            else:
                data = content.encode("utf-8")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"Content is {len(data)} bytes, over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload cap "
                    "for this connector; split the file or use a smaller excerpt."
                )
            uploaded = await client.upload(name, parent_id, data)
            return compact({"id": uploaded.get("id"), "name": uploaded.get("name")})

        return await run_tool("box_upload_file", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def box_update_file(id: FileId, name: str | None = None, parent_id: OptionalFolderId = None) -> str:
        """Rename and/or move (new parent_id) a file; only the fields given change. Only when explicitly asked."""

        async def action(client: BoxClient) -> Any:
            body = require(compact({"name": name, "parent": compact({"id": parent_id}) or None}))
            updated = await client.put(f"files/{id}", body)
            parent = updated.get("parent") or {}
            return compact({"id": updated.get("id"), "name": updated.get("name"), "parent_id": parent.get("id")})

        return await run_tool("box_update_file", client_factory, action)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def box_delete_file(id: FileId, confirm: Confirm = None) -> str:
        """Permanently delete a file. Irreversible; needs confirm: true. Only when the user explicitly asks."""

        async def action(client: BoxClient) -> Any:
            if confirm is not True:
                return not_confirmed(f"Permanently delete file {id}")
            await client.delete(f"files/{id}")
            return {"done": True, "deleted_file_id": id}

        return await run_tool("box_delete_file", client_factory, action)
