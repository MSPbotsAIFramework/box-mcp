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
"""Box HTTP status -> structured error envelope mapping (one fixed, finite vocabulary)."""

import json
from typing import Any

MAX_CHARS = 20_000


def compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def error_envelope(code: str, message: str, retryable: bool) -> str:
    return compact({"error": {"code": code, "message": message, "retryable": retryable}})


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("invalid_argument", False),
    412: ("invalid_argument", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class BoxError(Exception):
    """A Box API (or token-exchange) failure, carrying enough to build the error envelope.

    ``message`` must never include the request's credentials (client secret, Enterprise
    ID) -- callers that build this from a Box response body are responsible for that;
    this class does not inspect the message for secrets.
    """

    def __init__(self, status_code: int, message: str, box_code: str | None = None):
        self.status_code = status_code
        self.message = message
        self.box_code = box_code  # Box's own machine-readable error code, e.g. "folder_not_empty"
        super().__init__(f"Box API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)
