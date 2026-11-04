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
"""Box Client Credentials Grant (Service Account) token exchange.

Exchanged fresh on every request, never cached across requests or module-level: a
cache keyed by client id would let a stale or colliding entry hand one tenant's
access token to another (see "Option analysis" in docs/tickets/PRD-21298.md).
"""

from dataclasses import dataclass

import httpx

from .config import TOKEN_URL
from .errors import BoxError

HEADER_CLIENT_ID = "X-Box-Client-Id"
HEADER_CLIENT_SECRET = "X-Box-Client-Secret"
HEADER_ENTERPRISE_ID = "X-Box-Enterprise-Id"
REQUIRED_HEADERS = [HEADER_CLIENT_ID, HEADER_CLIENT_SECRET, HEADER_ENTERPRISE_ID]


@dataclass(frozen=True)
class BoxCredentials:
    client_id: str
    client_secret: str
    enterprise_id: str

    def __repr__(self) -> str:  # never leak the secret into logs or tracebacks
        return (
            f"BoxCredentials(client_id={self.client_id!r}, client_secret=<redacted>, "
            f"enterprise_id={self.enterprise_id!r})"
        )


async def exchange_token(credentials: BoxCredentials, http_client: httpx.AsyncClient) -> str:
    """POST the CCG grant and return the short-lived access token.

    Raises BoxError (mapped to the standard envelope by the caller) on a failed
    exchange. The error message never echoes the client secret.
    """
    try:
        resp = await http_client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "box_subject_type": "enterprise",
                "box_subject_id": credentials.enterprise_id,
            },
        )
    except httpx.TimeoutException as e:
        raise BoxError(0, "Timed out exchanging the Box Service Account token") from e
    except httpx.RequestError as e:
        raise BoxError(0, f"Network error exchanging the Box Service Account token: {type(e).__name__}") from e

    if resp.status_code >= 400:
        # Box's token endpoint answers a bad Client ID/Secret/Enterprise ID with HTTP 400
        # ("invalid_client" / "invalid_grant"), not 401 -- but from this connector's
        # perspective any failure to obtain a token means the supplied credentials did
        # not work, so it is always reported as `unauthorized`, never `invalid_argument`
        # (which would wrongly suggest a tool-call parameter problem).
        raise BoxError(401, _token_error_message(resp))

    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise BoxError(resp.status_code, "Box token response did not include access_token")
    return token


def _token_error_message(resp: httpx.Response) -> str:
    # Box's token-error body is {"error": "...", "error_description": "..."}; it never
    # contains the request's client_secret, so it is safe to surface as-is (truncated).
    fallback = f"Box token exchange failed with HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict):
        detail = payload.get("error_description") or payload.get("error")
        if detail:
            return f"{fallback}: {str(detail)[:300]}"
    return fallback
