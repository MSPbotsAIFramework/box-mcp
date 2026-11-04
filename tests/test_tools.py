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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from box_mcp.api_client import BoxError
from box_mcp.server import INSTRUCTIONS, create_mcp_server

ID = {"id"}

# Tool names are a public contract: existing agents and tenant connections depend on
# them. Changing this snapshot is a breaking change (SOP §13).
EXPECTED_TOOLS: dict[str, set[str]] = {
    "box_test_connection": set(),
    "box_get_folder_info": ID,
    "box_list_folder_items": ID,
    "box_create_folder": {"parent_id", "name"},
    "box_update_folder": ID,
    "box_delete_folder": ID,
    "box_get_file_info": ID,
    "box_get_file_download_url": ID,
    "box_upload_file": {"parent_id", "name", "content"},
    "box_update_file": ID,
    "box_delete_file": ID,
    "box_search": {"query"},
    "box_create_shared_link": {"item_type", "id", "access"},
    "box_get_shared_link": {"item_type", "id"},
}
DELETE_TOOLS = {n for n in EXPECTED_TOOLS if "_delete_" in n}
READ_TOOLS = {n for n in EXPECTED_TOOLS if n.startswith(("box_list_", "box_get_", "box_search")) or n == "box_test_connection"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


def test_tool_count_is_within_sop_guidance(tools):
    assert len(tools) <= 20


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_names_use_the_vendor_prefix(tools, name):
    assert name.startswith("box_")


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is (name in READ_TOOLS)
    assert bool(annotations.destructiveHint) is (name in DELETE_TOOLS)


@pytest.mark.parametrize("name", sorted(set(EXPECTED_TOOLS) - READ_TOOLS))
def test_write_tools_say_when_to_use_them(tools, name):
    assert "explicitly ask" in tools[name].description


@pytest.mark.parametrize("name", sorted(DELETE_TOOLS))
def test_delete_tools_take_confirm(tools, name):
    assert "confirm" in tools[name].inputSchema["properties"]


def test_delete_folder_also_takes_recursive(tools):
    assert "recursive" in tools["box_delete_folder"].inputSchema["properties"]


def test_list_and_search_tools_declare_a_limit_param(tools):
    paged = [t for t in tools.values() if "limit" in t.inputSchema["properties"]]
    assert len(paged) == 2  # box_list_folder_items, box_search
    for tool in paged:
        assert tool.inputSchema["properties"]["limit"]["anyOf"][0]["minimum"] == 1, tool.name


def test_clamp_limit_caps_at_200():
    from box_mcp.tools._common import clamp_limit

    assert clamp_limit(500) == 200
    assert clamp_limit(None) == 25
    assert clamp_limit(10) == 10


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (409, "invalid_argument", False),
        (422, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(BoxError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("box_test_connection", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"
