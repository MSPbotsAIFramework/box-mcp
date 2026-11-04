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
"""Connection check."""

from mcp.server.fastmcp import FastMCP

from ..api_client import BoxClient
from ._common import READ_ONLY, ClientFactory, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def box_test_connection() -> str:
        """Check the Box connection and show the authenticated Service Account."""

        async def action(client: BoxClient) -> dict:
            me = await client.get("users/me")
            return {
                "status": "ok",
                "user": {"id": me.get("id"), "name": me.get("name"), "login": me.get("login")},
            }

        return await run_tool("box_test_connection", client_factory, action)
