# PRD-21298 · Code review

## Round 1 · 2026-11-04 · reviewed dev/PRD-21298 @ f0f841f (base main @ e0af09a)

Verdict: changes requested

### Findings

1. [must-fix] src/box_mcp/api_client.py:100-117 (`BoxClient.redirect_location`, used by `box_get_file_download_url`) and src/box_mcp/api_client.py:119-139 (`BoxClient.upload`, used by `box_upload_file`) call `self._http` directly instead of going through `BoxClient._request()`, so neither tool gets the retry-with-backoff that every other tool in this connector gets. This contradicts behavior the connector itself documents: the tech design's change list for `api_client.py` says "retry-with-backoff on 429/5xx for reads, 429-only for writes, honoring `Retry-After`" (docs/tickets/PRD-21298.md:203), and README.md:143-144 tells callers "Reads are retried with backoff on 429, 5xx and network errors; writes only on 429." `box_get_file_download_url` is a read tool and gets none of that; `box_upload_file` is a write tool and also gets none of that (no retry on 429, no honoring of `Retry-After`). In production, Box's upload endpoint and its file-content redirect endpoint both return 429 under ordinary load, so these two tools will fail on a single rate-limit response while every other tool in the same connector retries and very likely succeeds — an inconsistency a caller has no way to predict from the README. Neither method is unhandled-exception-unsafe (the generic `except Exception` in `tools/_common.py:run_tool` still turns a stray `httpx` exception into an `upstream_error` envelope), so this is a reliability/consistency gap, not a crash. TC21 (AC16) only exercises `box_test_connection`'s retry path, so the gap is not caught by the existing test suite. Fix: route `redirect_location` and `upload` through `_request()` (or share its retry loop) so both get the same backoff policy as the rest of the connector, and add at least one retry test for one of the two bypassed tools so a future regression is caught.

### Fixes
<!-- filled in by the dev leg on rework -->
