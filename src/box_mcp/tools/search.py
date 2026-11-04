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
"""Keyword search across Box content."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import BoxClient
from ._common import READ_ONLY, ClientFactory, Limit, Offset, compact, list_page, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def box_search(
        query: Annotated[str, Field(description="Keyword(s) to search for.", min_length=1)],
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Search files and folders in Box's search index by keyword. Paginated like the list tools."""

        async def action(client: BoxClient) -> dict:
            page = await list_page(client, "search", "entries", {"query": query}, limit, offset)
            page["entries"] = [compact({"id": e.get("id"), "name": e.get("name"), "type": e.get("type")}) for e in page["entries"]]
            return page

        return await run_tool("box_search", client_factory, action)
