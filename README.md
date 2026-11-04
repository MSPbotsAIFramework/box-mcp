# Box MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Box](https://www.box.com) API
(cloud content/file storage). It lets an AI agent browse folders, read file/folder
metadata, download file content (via a time-limited URL), upload new files, rename or
move files and folders, delete files and folders (with confirmation), search Box
content by keyword, and create or inspect a shared link for a file or folder, for one
Box enterprise reachable via a Service Account.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and
  are never stored; a fresh Box access token is exchanged on every request and never
  cached across requests
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Box-Client-Id` | Yes | string | none | - | Box app Client ID | `abc123...` |
| `X-Box-Client-Secret` | Yes | password | none | - | Box app Client Secret | `shh...` |
| `X-Box-Enterprise-Id` | Yes | string | none | - | Box Enterprise ID the Service Account authenticates as | `123456789` |

Where to get them: in the [Box Developer Console](https://app.box.com/developers/console),
create (or open) a Custom App configured for the **Server Authentication (Client
Credentials Grant)** app type, then authorize it for your enterprise. The Client ID and
Client Secret are on the app's Configuration page; the Enterprise ID is on the
enterprise's General Settings page in the Box Admin Console.

How credentials are used: on every request this server exchanges these three values for
a short-lived Box access token (`POST https://api.box.com/oauth2/token`,
`grant_type=client_credentials`) and uses that token for the Box API calls the tool
needs. The token is never cached or reused across requests.

A request to `/mcp` missing any of the three headers gets HTTP 401 naming which one(s).

## Tools

All tools return a JSON string. `box_list_folder_items` and `box_search` take `limit`
(default 25, silently clamped to 200) and `offset` (0-based; pass `next_offset` from the
previous result to continue) and return `{count, total_count, limit, offset, has_more,
next_offset}` alongside their items. **write** marks tools that change data; **confirm**
marks deletes, which also need `confirm: true` (`box_delete_folder` also needs
`recursive: true` to delete a folder that still has items in it).

| Area | Tools |
|---|---|
| Connection | `box_test_connection` |
| Folders | `box_get_folder_info` (**id**), `box_list_folder_items` (**id**, limit, offset); **write**: `box_create_folder` (**parent_id**, **name**), `box_update_folder` (**id**, name, parent_id); **confirm**: `box_delete_folder` (**id**, recursive) |
| Files | `box_get_file_info` (**id**), `box_get_file_download_url` (**id**); **write**: `box_upload_file` (**parent_id**, **name**, **content**, encoding), `box_update_file` (**id**, name, parent_id); **confirm**: `box_delete_file` (**id**) |
| Search | `box_search` (**query**, limit, offset) |
| Sharing | `box_get_shared_link` (**item_type**, **id**); **write**: `box_create_shared_link` (**item_type**, **id**, **access**) |

The folder id for the root of an enterprise's content is `0`.

`box_upload_file`'s `content` is plain text by default; pass `encoding: "base64"` to
upload binary content. Files over 10 MB are rejected before any upload request is made
(base64-encoding a 50 MB file into a single tool call is impractical for an LLM-driven
caller; Box's own simple-upload ceiling is 50 MB).

`box_get_file_download_url` never returns file bytes, only a time-limited download URL.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "not_found", "message": "Box returned HTTP 404 for folders/12345: Not Found", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Reads are retried with backoff on 429, 5xx and network
errors; writes only on 429.

## Run locally

```bash
uv sync
uv run python -m box_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t box-mcp:dev .
docker run --rm -p 8080:8080 box-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default
`0.0.0.0`), `MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials
are never read from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Box-Client-Id: your-client-id" \
  -H "X-Box-Client-Secret: your-client-secret" \
  -H "X-Box-Enterprise-Id: your-enterprise-id" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# items in the root folder
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Box-Client-Id: your-client-id" \
  -H "X-Box-Client-Secret: your-client-secret" \
  -H "X-Box-Enterprise-Id: your-enterprise-id" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"box_list_folder_items","arguments":{"id":"0"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Service Account only (v1).** This connector authenticates as the Box enterprise's
  Service Account (Client Credentials Grant), not as a specific Box end user ("App
  User" / As-User impersonation). The ticket named no per-user access requirement;
  adding App User support later is an additive, non-breaking change (one more optional
  credential header). See `docs/tickets/PRD-21298.md` "Open questions".
- **No token caching, by design.** A fresh access token is exchanged on every request
  and held only for that one request's Box calls (never at module level, never reused
  across requests) — see "Option analysis" in `docs/tickets/PRD-21298.md`.
- **10 MB upload cap (v1).** Well under Box's own 50 MB simple-upload ceiling; the
  chunked-upload API for larger files is deferred (not requested).
- **14 tools** covering browse/read/create/update/delete/search/share. Comments,
  tasks, collaborations, webhooks, versions, trash/restore, retention/legal-hold and
  metadata templates are out of scope for v1 (none were requested).
- **Deletes need confirmation.** `box_delete_file` and `box_delete_folder` take
  `confirm: true`; `box_delete_folder` also needs `recursive: true` to remove a folder
  that still has items in it.
- **Field names are drawn from Box's published API reference**, not a live call (no
  Box test tenant was available during design or dev — see `docs/tickets/PRD-21298.md`
  "Risks & dependencies"): `item_count` comes from a folder's
  `item_collection.total_count`; list/search pagination uses Box's native
  `limit`/`offset`/`total_count`, not a page number. Confirm against a real response
  once a Service Account is available (see the `manual` test cases in
  `docs/tickets/PRD-21298.ac.json`).

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
