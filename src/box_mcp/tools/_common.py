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
"""Shared argument types, the limit/offset pagination helper, and the error-handling wrapper."""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ..api_client import BoxClient
from ..config import DEFAULT_LIMIT, MAX_LIMIT
from ..errors import BoxError, error_envelope

logger = logging.getLogger("box_mcp.tools")

ClientFactory = Callable[[], BoxClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Box credentials are missing for this request (X-Box-Client-Id, X-Box-Client-Secret, X-Box-Enterprise-Id).",
    False,
)

FolderId = Annotated[str, Field(description="Box folder id. The root folder is '0'.")]
OptionalFolderId = Annotated[str | None, Field(description="Box folder id. The root folder is '0'.")]
FileId = Annotated[str, Field(description="Box file id.")]
Limit = Annotated[
    int | None,
    Field(description=f"Max items to return (default {DEFAULT_LIMIT}; silently clamped to {MAX_LIMIT}).", ge=1),
]
Offset = Annotated[int | None, Field(description="0-based position to start from; pass next_offset from the previous result to continue.", ge=0)]
Confirm = Annotated[bool | None, Field(description="Must be true, set only after the user approved this exact action.")]
Recursive = Annotated[
    bool | None,
    Field(description="Must be true together with confirm to delete a folder that still has items in it."),
]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def require(body: dict) -> dict:
    if not body:
        raise ValueError("Nothing to update: pass at least one field to change.")
    return body


def clamp_limit(limit: int | None) -> int:
    return min(max(limit or DEFAULT_LIMIT, 1), MAX_LIMIT)


async def list_page(
    client: BoxClient,
    path: str,
    key: str,
    params: dict[str, Any],
    limit: int | None,
    offset: int | None,
) -> dict:
    """GET a Box list endpoint and return {<key>, count, limit, offset, has_more, next_offset}.

    Box's own list endpoints already page by limit/offset and report total_count, so
    this only clamps the limit (AC4 TC6) and derives has_more/next_offset from it.
    """
    effective_limit = clamp_limit(limit)
    effective_offset = offset or 0
    payload = await client.get(path, compact(params) | {"limit": effective_limit, "offset": effective_offset})
    items = payload.get(key) if isinstance(payload, dict) else payload
    items = items if isinstance(items, list) else []
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if isinstance(total_count, int):
        has_more = effective_offset + len(items) < total_count
    else:
        has_more = len(items) >= effective_limit
    return compact(
        {
            key: items,
            "count": len(items),
            "total_count": total_count,
            "limit": effective_limit,
            "offset": effective_offset,
            "has_more": has_more,
            "next_offset": effective_offset + len(items) if has_more else None,
        }
    )


def not_confirmed(action: str, extra: str = "") -> str:
    """The structured invalid_argument error for a destructive call missing confirm (AC7/AC12)."""
    message = f"Not done. {action} needs confirm: true"
    if extra:
        message += f" {extra}"
    message += ", set only after the user approved this exact action."
    return error_envelope("invalid_argument", message, False)


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[BoxClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except BoxError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"done": True}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


MAX_CHARS = 20_000


def _dump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def dump_json_capped(data: Any, max_chars: int = MAX_CHARS) -> str:
    """Serialize ``data`` within ``max_chars``; truncates the largest list field, never partial records."""
    text = _dump(data)
    if len(text) <= max_chars or not isinstance(data, dict):
        return text

    list_keys = [k for k, v in data.items() if isinstance(v, list)]
    if not list_keys:
        return text
    key = max(list_keys, key=lambda k: len(_dump(data[k])))
    items = data[key]

    def build(n: int) -> dict:
        candidate = {k: v for k, v in data.items() if k not in ("next_offset",)}
        candidate[key] = items[:n]
        candidate["count"] = n
        candidate["has_more"] = True
        candidate["truncated"] = True
        candidate["note"] = (
            f"Only {n} of {len(items)} records fit the response size limit; call again with a "
            f"smaller limit and the same offset to see the rest."
        )
        return candidate

    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_dump(build(mid))) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    if lo > 0:
        return _dump(build(lo))
    return _dump({"truncated": True, "note": f"Result too large to return ({len(text)} chars); narrow the query."})
