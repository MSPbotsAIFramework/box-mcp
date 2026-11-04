# PRD-21298 · Code review

Status: frozen @ 78fa004

## Round 1 · 2026-11-04 · reviewed dev/PRD-21298 @ f0f841f (base main @ e0af09a)

Verdict: changes requested

### Findings

1. [must-fix] src/box_mcp/api_client.py:100-117 (`BoxClient.redirect_location`, used by `box_get_file_download_url`) and src/box_mcp/api_client.py:119-139 (`BoxClient.upload`, used by `box_upload_file`) call `self._http` directly instead of going through `BoxClient._request()`, so neither tool gets the retry-with-backoff that every other tool in this connector gets. This contradicts behavior the connector itself documents: the tech design's change list for `api_client.py` says "retry-with-backoff on 429/5xx for reads, 429-only for writes, honoring `Retry-After`" (docs/tickets/PRD-21298.md:203), and README.md:143-144 tells callers "Reads are retried with backoff on 429, 5xx and network errors; writes only on 429." `box_get_file_download_url` is a read tool and gets none of that; `box_upload_file` is a write tool and also gets none of that (no retry on 429, no honoring of `Retry-After`). In production, Box's upload endpoint and its file-content redirect endpoint both return 429 under ordinary load, so these two tools will fail on a single rate-limit response while every other tool in the same connector retries and very likely succeeds — an inconsistency a caller has no way to predict from the README. Neither method is unhandled-exception-unsafe (the generic `except Exception` in `tools/_common.py:run_tool` still turns a stray `httpx` exception into an `upstream_error` envelope), so this is a reliability/consistency gap, not a crash. TC21 (AC16) only exercises `box_test_connection`'s retry path, so the gap is not caught by the existing test suite. Fix: route `redirect_location` and `upload` through `_request()` (or share its retry loop) so both get the same backoff policy as the rest of the connector, and add at least one retry test for one of the two bypassed tools so a future regression is caught.

### Fixes

Round 1, must-fix 1 — fixed @ a33f75b:

`BoxClient.redirect_location()` and `BoxClient.upload()` called `self._http` directly, bypassing `_request()`'s retry loop entirely. Extracted the loop into a new `BoxClient._send_with_retry(method, log_path, send)` that takes the actual HTTP call as a callback and applies the same 429/5xx/network-error backoff policy regardless of transport shape. `_request()` now calls it for the JSON-body path; `redirect_location()` calls it for the no-redirect GET (`follow_redirects=False`) before doing its own 3xx/`Location`-header handling; `upload()` calls it for the multipart POST before doing its own entries/payload handling. All three still log the same `box_api method=... path=... status=... ms=... attempt=...` line per attempt.

Both fixed call sites keep their original method classification for the retry policy: `redirect_location` is `GET` (retries on 429, 5xx, timeout, network error, same as every other read), `upload` is `POST` (retries on 429 only, same as every other write) — matching README.md:143-144 and the tech design's documented policy.

Added two regression tests in `tests/test_api_client.py`: `test_get_file_download_url_retries_on_429` and `test_upload_file_retries_on_429`, each replaying a 429-then-success sequence and asserting the tool still succeeds and that Box was called twice.

Gates rerun on `a33f75b` (synced with `main` @ e0af09a, unchanged since Round 1): `uv run pytest` equivalent (`.venv/bin/python -m pytest -q`) — 118 passed, 0 failed (116 before this round's two new tests). No new Chinese characters (`grep -rnP '[\x{4e00}-\x{9fff}]'` over the tree, excluding `.git`/`.venv`/`.pytest_cache`, is empty). README Connector section and `tools/list` shape unchanged (not touched by this fix; both still covered by `tests/test_tools.py` and `tests/test_middleware.py`, included in the 118). `gitleaks` is not installed on this machine, so it was not run; this fix does not touch credential handling.

No other changes made; this round only touches `src/box_mcp/api_client.py` and `tests/test_api_client.py`.

## Round 2 · 2026-11-04 · reviewed dev/PRD-21298 @ 78fa004 (base main @ e0af09a)

Verdict: approved

### Round 1 fix verified

Round 1's must-fix 1 (`redirect_location`/`upload` bypassing the retry loop) is fixed correctly. Diffed `src/box_mcp/api_client.py` at `7f83101..78fa004`: both call sites now route through the new `BoxClient._send_with_retry(method, log_path, send)`, which is the same loop `_request()` itself now calls — so there is exactly one retry/backoff implementation, not three. `redirect_location` passes `method="GET"` (reads retry on 429/5xx/timeout/network-error, matching README.md:143-144) and `upload` passes `method="POST"` (writes retry only on 429) — the method classification each tool needs for the documented policy is preserved. `_request()`'s own behavior (status>=400 -> `BoxError`, empty body -> `None`, non-JSON body -> `{"raw_response": ...}`) is unchanged, just now expressed in terms of the shared `send()` callback. The two new tests (`test_get_file_download_url_retries_on_429`, `test_upload_file_retries_on_429` in `tests/test_api_client.py`) each replay a 429-then-success sequence through the real tool call and assert Box was hit twice — independently re-ran `uv run pytest -q` on this worktree (not just read the claimed count): **118 passed, 0 failed**, matching the `[DEV]` stub.

### New diff since Round 1 (`7f83101..78fa004`)

Scope is exactly `src/box_mcp/api_client.py`, `tests/test_api_client.py`, and this review doc — no other file changed. No new findings:

- Retry policy (`retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))`) is unchanged from Round 1, only relocated into `_send_with_retry`; it still matches README.md's documented behavior for both reads and writes.
- `_send_with_retry`'s own exception handling (`httpx.TimeoutException` / `httpx.RequestError` -> `BoxError(0, ...)`) matches what `upload()`'s old inline try/except did, so the error messages/semantics for upload timeouts and network errors are preserved, not changed.
- No credential handling, auth, middleware, error-code mapping, or tool surface touched by this round — independently re-ran the non-ASCII scan (`grep -rnP '[\x{4e00}-\x{9fff}]'` over the tree, excluding `.git`/`.venv`/`.pytest_cache`): no output, confirming the Fixes section's claim.
- README.md's Connector section (verbatim-diffed against `vendor-mcp-development/reference/readme-connector-section.md`) and the Authentication headers / Tools tables were not touched by this round and still match the code (14 tools, 3 credential headers, retry-policy line).

No must-fix, no suggestions. Full implementation (Round 1's scope) was also read in this round since this is effectively the first complete review of the code surface area: auth/token exchange (`auth.py`, never cached, per-request `ContextVar`), middleware isolation (`server.py`, tested concurrently for AC17 including at the token level in `test_concurrent_tool_calls_never_cross_tenant_tokens`), delete-confirm guards (`box_delete_file`/`box_delete_folder`), the 10 MB upload cap, pagination clamp, and the error envelope mapping (`errors.py`) all match the tech design and README with no discrepancies found.
