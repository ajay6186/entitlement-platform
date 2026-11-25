# Customer Entitlement & Support Case Platform

One governed REST API over three systems of record (a Salesforce-like case system, a NetSuite-like entitlement
system and a Snowflake-like usage warehouse), an MCP server that lets an AI support copilot use the same
capabilities safely as the signed-in user, a Claude-powered chat UI, and signed webhooks with a live delivery
dashboard.

```
                         ┌───────────────── platform API (FastAPI, :8000) ─────────────────┐
case system ──────┐      │  REST /v1 · authz & business rules · idempotency · audit         │──> apps / workflows
entitlements ─────┼─────>│  resilience (timeouts, retries, circuit breakers)                │
usage warehouse ──┘      │  transactional outbox ──> signed webhooks (HMAC-SHA256)          │──> webhook-receiver (:8200)
 (mock upstreams, :9100) └──────────────────────────────────────────────────────────────────┘        │ SSE push
                                  ▲                               ▲                                    ▼
                    MCP server (stdio / HTTP :8001)        React UI (:5173): Chat · Webhooks dashboard · notifications
                                  ▲                               │
                    chat backend (:8100) ── Claude ◄──────────────┘
```

The REST API owns every business rule and authorization decision. MCP is a thin adapter on top of it, so an
agent can never do more than the human it acts for, and every write the chatbot proposes waits for a human click.

## Contents
- [Quick start](#quick-start) · [Services and URLs](#services-and-urls) · [Configuration](#configuration)
- [Using the API](#using-the-api) · [Support Assistant chat](#support-assistant-chat) · [Webhooks](#webhooks)
- [MCP server](#mcp-server) · [Tests](#tests) · [Debugging in VS Code](#debugging-in-vs-code) · [Postman](#postman)
- [Project layout](#project-layout) · [Troubleshooting](#troubleshooting) · [What was verified](#what-was-verified)

## Quick start

### Option A: Docker (whole stack, recommended)

Requirements: Docker Desktop (Compose v2).

```bash
cp .env.example .env              # then set ANTHROPIC_API_KEY in .env (only needed for the chat)
docker compose up --build -d --wait
docker compose ps                 # all 6 services should be "healthy"
```

Open http://localhost:5173 → **Settings → Quick sign-in → `support_lead` → Get token → Save**, and ask
*"Find Platinum customers with open P1 cases."* Stop with `docker compose down` (add `-v` to wipe the data volumes).

### Option B: local Python (API only)

Requirements: Python 3.12+ (Node 20+ for the UI).

```bash
python -m venv venv && venv/Scripts/activate     # Windows; on macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --port 8000
```

Open http://localhost:8000/docs. The mock upstreams run in-process; no credentials or network access are needed.
To run the chat locally as well, see [Support Assistant chat](#support-assistant-chat).

## Services and URLs

Started by [docker-compose.yml](docker-compose.yml). Containers reach each other by service name; each waits for its
dependencies' health checks.

| Service | URL on your machine | What it is | Talks to |
|---|---|---|---|
| `frontend` | http://localhost:5173 | React UI: Chat, Webhooks dashboard (`/#webhooks`), notifications | `chat`, `api`, `webhook-receiver` (from the browser) |
| `chat` | http://localhost:8100/health | Chat backend: Claude + MCP client, approval of write actions | `api`; Claude API; starts `mcp_server` over stdio per request |
| `api` | http://localhost:8000/docs | Platform REST API (Swagger renders [openapi.yaml](openapi.yaml)) | `upstreams:9000`, `webhook-receiver:8200` |
| `mcp` | http://localhost:8001/mcp | MCP server over streamable HTTP (fixed `support_lead` dev identity) | `api:8000` |
| `upstreams` | http://localhost:9100/docs | Mock systems of record with fault injection | — |
| `webhook-receiver` | http://localhost:8200/docs | Demo "customer system" receiving the API's webhooks | — |

`upstreams` is published on host port **9100** (container port 9000) because Jupyter/VS Code kernels often hold
9000–9004 on Windows; change it with `UPSTREAMS_HOST_PORT`.

## Configuration

All settings have local-development defaults; [.env.example](.env.example) lists them.

| Variable | Used by | Default / notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | chat | **Required for the chat.** Read from `.env` or your shell; only the `chat` container receives it |
| `CHAT_MODEL` | chat | `claude-opus-5-5` |
| `PLATFORM_JWT_SECRET`, `PLATFORM_WEBHOOK_SIGNING_SECRET` | api | obviously fake dev values; use a secret manager elsewhere |
| `PLATFORM_FIXED_NOW` | api | `2026-11-23T10:00:00Z`: pins "business now" so case ages and renewal windows are reproducible |
| `PLATFORM_UPSTREAM_BASE_URL` | api | unset = in-process mocks; Docker sets `http://upstreams:9000` |
| `PLATFORM_WEBHOOK_DEFAULT_SUBSCRIBER_URL`, `PLATFORM_WEBHOOK_DEFAULT_EVENT_TYPES` | api | default subscription; Docker points it at `webhook-receiver` for both case events |
| `PLATFORM_ENABLE_DEV_ENDPOINTS` | api | `true`; set `false` outside local development (removes `/dev/*`) |
| `PLATFORM_CORS_ORIGINS` | api | `http://localhost:5173,…` (browser access for the dashboard) |
| `WEBHOOK_SECRET` | webhook-receiver | must equal `PLATFORM_WEBHOOK_SIGNING_SECRET` |

`.env` is git-ignored and Docker-ignored: it is never committed or copied into an image.

## Using the API

Personas (dev-only stand-in for the IdP, `POST /dev/token`):

| Persona | Scopes |
|---|---|
| `support_agent` | read cases and customers, comment |
| `support_lead` | + escalate |
| `finance_analyst` | customers including ARR (`customers:financials`) |
| `platform_admin` | webhooks (`webhooks:manage`), audit, read cases |

```bash
TOKEN=$(curl -s -X POST localhost:8000/dev/token -H 'Content-Type: application/json' \
          -d '{"persona":"support_lead"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s localhost:8000/v1/customers/ACC-007 -H "Authorization: Bearer $TOKEN"
curl -s "localhost:8000/v1/cases?priority=P1&status=open&support_tier=Platinum&limit=3" -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/v1/cases/CS-10232/comments -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-key-0001' -d '{"body":"We are investigating."}'
# If-Match is optional; send the case's ETag (e.g. "5") to get 412 instead of overwriting a newer version
curl -s -X POST localhost:8000/v1/cases/CS-10231/escalate -H "Authorization: Bearer $TOKEN" \
     -H 'If-Match: "5"' -H 'Content-Type: application/json' -d '{"reason":"Critical production outage"}'

# make the usage warehouse fail, then see a degraded (still 200) customer view; then restore everything
curl -s -X POST localhost:8000/dev/upstream-faults -H 'Content-Type: application/json' -d '{"system":"usage_warehouse","mode":"down"}'
curl -s localhost:8000/v1/customers/ACC-007 -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/dev/reset
```

In Swagger, use `POST /dev/token`, then **Authorize** with the `access_token`. From the CLI:
`python -m scripts.mint_token support_lead`. `/dev/upstream-faults` and `/dev/reset` work with in-process mocks
and with the standalone `upstreams` service (they forward to its `/_admin/*`).

## Support Assistant chat

A React chat UI ([frontend/](frontend/)) and a FastAPI chat backend ([chat_backend/app.py](chat_backend/app.py)) in
which Claude answers questions by calling the MCP server.

Browser → chat backend (:8100) → Claude ⇄ MCP client (stdio) → `mcp_server.server` → platform API (:8000)

- Replies are rendered as Markdown (tables, lists) in a fixed layout: answer first, table, *Needs attention*,
  *Flagged* content, next step. Raw HTML is never rendered, so case text stays text.
- The user's platform token goes to the MCP server process as `MCP_ACCESS_TOKEN`; the model never sees it, and the
  API enforces the user's scopes. The Anthropic key stays on the chat backend.
- Write actions never run from a model tool call. `add_case_comment` and `escalate_case` become a pending action
  with **Approve / Reject** buttons; only the click runs it (single use, same user, expires after 10 minutes). For
  escalation the click is passed to the MCP server as its confirmation answer, so the server re-checks the case,
  scopes and version before escalating.
- `GET /health` reports `claude_credentials: true/false`. A missing key gives a clear 503, not a generic 502.
- Limits: chat history lives in the browser and pending actions in backend memory (lost on restart); one MCP
  process is started per request.

Without Docker:

```bash
uvicorn src.main:app --port 8000                                  # terminal 1: platform API
ANTHROPIC_API_KEY=... uvicorn chat_backend.app:app --port 8100     # terminal 2: chat backend
cd frontend && npm install && npm run dev                          # terminal 3: UI on http://localhost:5173
```

## Webhooks

The platform publishes signed webhooks through a transactional outbox; the separate **webhook-receiver** service
consumes them like a customer's system would. The UI's **Webhooks** tab (http://localhost:5173/#webhooks) shows
both sides live, and every received webhook pops up as a notification.

| Event | Sent when |
|---|---|
| `case.escalated` | a case is escalated (API, Swagger, or the chat's Approve button) |
| `case.comment_added` | a comment is added (ids only; the comment text stays in the case system) |
| `webhook.test` | *Send test event* on the dashboard (`POST /v1/webhooks/test`) |

**Sender** ([src/services/webhook_service.py](src/services/webhook_service.py)):
- Event and delivery rows are committed in the same transaction as the audit record, then sent after the response.
- Signature: `Webhook-Signature: v1=hex(HMAC-SHA256(secret, "{Webhook-Id}.{Webhook-Timestamp}.{body}"))`.
- Up to 3 attempts with exponential backoff; 4xx (except 408/429) is permanent. Every attempt is stored: headers
  sent, status or error, response excerpt, duration.
- Failed deliveries can be re-sent (`POST /v1/webhooks/deliveries/{id}/retry`); deliveries left `pending` by a
  stopped process are resumed on start-up.
- Dashboard API (`webhooks:manage`): `GET /v1/webhooks/stats`, `/events`, `/events/{id}`, `/subscriptions`,
  `PATCH /subscriptions/{id}`, `POST /test`.

**Receiver** ([webhook_receiver/](webhook_receiver/)):
- Verifies the signature with its own code, rejects timestamps older than 5 minutes, de-duplicates on
  `Webhook-Id`, and stores every request in SQLite (`GET /events`).
- `PUT /control` simulates failures: `fail_next` (500 twice, then recovers), `fail` (always 500), `slow` (8 s,
  longer than the sender's 5 s timeout), `reject` (400).
- `GET /events/stream` pushes each received request to open browsers (Server-Sent Events). The UI shows a pop-up
  (received, duplicate ignored, rejected, receiver failed); the header bell enables desktop notifications for
  when the tab is in the background, and its dot is green while the stream is connected.

**Demo script**
1. Open http://localhost:5173, sign in as `support_lead`. In a second tab open http://localhost:5173/#webhooks and
   click *Sign in as platform_admin* (the dashboard keeps its own token).
2. In Chat: *"Escalate CS-10231 because checksum errors risk data loss."* → **Approve**. A green pop-up appears;
   the event is **delivered** and the receiver shows **accepted** with a valid signature.
3. Set the receiver to **Fail next 2**, then add a comment in Chat and approve it: attempts 1 and 2 fail with
   500, attempt 3 succeeds. Click the event to see the timeline.
4. Set **Down (500)** and send a test event: it fails after 3 attempts. Switch back to **Healthy** and click
   **Retry**: delivered, with the attempt marked *manual retry*.
5. Set **Slow (8 s)** and send a test event: all 3 attempts time out on the sender's side, so the delivery is
   **failed**, yet the receiver processed the first request and flagged the other two as **duplicate**. Retry
   when healthy: delivered, and the receiver answers *duplicate* without processing it twice. A timeout does not
   mean "not received", which is why receivers de-duplicate on `Webhook-Id`.

Reset afterwards: `POST /dev/reset` (cases) and **Clear** on the receiver panel.

## MCP server

```bash
python -m mcp_server.demo          # scripted agent answers the sample question over stdio -> docs/demo_transcript.md
python -m mcp_server.demo --llm    # Claude chooses the tool calls; needs ANTHROPIC_API_KEY
```

Tools: `search_cases`, `get_customer_summary`, `add_case_comment`, `escalate_case` (requires human confirmation).
To use the server from an MCP host (for example Claude Desktop):

```json
{"mcpServers": {"entitlement-platform": {
  "command": "python", "args": ["-m", "mcp_server.server"], "cwd": "<path to this repo>",
  "env": {"MCP_DEV_PERSONA": "support_lead"}}}}
```

In production the host passes the user's delegated token in `MCP_ACCESS_TOKEN` instead of `MCP_DEV_PERSONA`, and
`PLATFORM_BASE_URL` points at the deployed API. Without `PLATFORM_BASE_URL` the platform runs embedded.

The server also runs over streamable HTTP (the `mcp` container, or
`python -m mcp_server.server --transport http` on http://127.0.0.1:8001/mcp). One fixed identity per process, so
local use only. Browse it with `npx @modelcontextprotocol/inspector`.

Tools are declared with FastMCP's `@mcp.tool()` in [mcp_server/server.py](mcp_server/server.py); the JSON Schema
comes from the type hints (`Literal` enums, `Field` limits and patterns) and unknown arguments are rejected. To add
a tool: write the logic as a plain async function in [mcp_server/tools.py](mcp_server/tools.py), then declare it in
`create_server()` with a description, `ToolAnnotations` and a body that calls `run(...)` (rate limit, audit, errors).

## Tests

```bash
python -m pytest            # 114 tests, about 60 s (each test gets a fresh app and SQLite DB)
```

| File | Covers |
|---|---|
| [test_customers.py](src/tests/test_customers.py) | 200/404/422, ARR redaction, degraded on usage down or timeout, retry on flaky upstream, 503 on entitlement down, circuit breaker opening, 401/403 |
| [test_cases.py](src/tests/test_cases.py) | filters, sort, cross-system tier filter, 422 on invalid or unknown filters, cursor pagination (no gaps, cursor bound to query), escalation 403/409/412/concurrent-modification/success with audit and webhook |
| [test_idempotency.py](src/tests/test_idempotency.py) | create, duplicate key (replayed, no duplicate), key reuse with a different body (422), per-caller scope, retry after upstream failure |
| [test_security.py](src/tests/test_security.py) | webhook signature valid, invalid, tampered, replayed and duplicate; expired, forged and `alg=none` tokens; agent scope ceiling; agent audit; correlation IDs; 429 |
| [test_contract.py](src/tests/test_contract.py) | route parity with openapi.yaml; 39 real responses (including errors) validated against the documented schemas |
| [test_webhook_dashboard.py](src/tests/test_webhook_dashboard.py) | event list and detail with signed attempt log, stats, comment event published once (not on replay), test events, manual retry, enable/disable, permissions, start-up resumption of pending deliveries |
| [test_webhook_receiver.py](src/tests/test_webhook_receiver.py) | receiver: accepted then duplicate, wrong secret, old timestamp, tampered body, `fail_next` recovery, `reject`, live push to listeners |
| [test_mcp_evals.py](src/tests/test_mcp_evals.py) | every MCP evaluation scenario, over a real MCP client/server session ([TESTEVALS.md](TESTEVALS.md)) |

Frontend: `cd frontend && npm run build` type-checks (`tsc -b`) and builds.

## Debugging in VS Code

[.vscode/launch.json](.vscode/launch.json) has two groups:

- **Local (your venv):** *API: uvicorn*, *Chat backend: uvicorn*, *Tests: pytest*, *MCP: demo*,
  *MCP: server (HTTP :8001)*, and the *API + Chat backend* compound.
- **Docker (attach):** *Docker: up (debug) + attach all* starts the stack with
  [docker-compose.debug.yml](docker-compose.debug.yml) (task *docker: up (debug)*) and attaches to every Python
  service. Source folders are bind-mounted so breakpoints match your working copy; restart a service to pick up
  code changes.

| Service | debugpy port |
|---|---|
| api | 5678 |
| upstreams | 5679 |
| mcp | 5680 |
| chat (also follows its MCP subprocess) | 5681 |
| webhook-receiver | 5682 |

Tasks ([.vscode/tasks.json](.vscode/tasks.json)): `docker: up`, `docker: up (debug)`, `docker: logs`, `docker: down`.

## Postman

Import [postman/collection.json](postman/collection.json) and [postman/environment.json](postman/environment.json),
start the API, and run the collection in order (32 requests with functional and regression tests). Or run
`npx newman run postman/collection.json -e postman/environment.json`. The files are generated by
`python -m scripts.build_postman`. The webhook dashboard endpoints are not in the collection yet; use Swagger.

## Project layout

```
openapi.yaml              API contract (source of truth; served at /docs, /openapi.json, /openapi.yaml)
src/main.py               app factory and lifespan wiring (DB, upstream client, webhook dispatcher, CORS)
src/api/                  HTTP layer: parsing, status codes, headers
src/services/             business rules: aggregation, search, idempotency, escalation, audit, webhooks
src/clients/              upstream clients: timeouts, retries, circuit breaker, format mapping
src/upstreams/            mock systems of record with fault injection (in-process or the `upstreams` service)
src/models/               SQLAlchemy tables (idempotency, audit, outbox events, webhook subscriptions/deliveries/attempts/inbox)
src/core/                 auth, problem+json errors, logging, middleware, rate limiting, clock
src/tests/                pytest suite
mcp_server/               MCP server (FastMCP tools), tool logic, guardrails, demo
chat_backend/             Support Assistant chat API (Claude + MCP client, approval of write actions)
webhook_receiver/         demo webhook consumer: signature check, de-duplication, history, failure simulation, SSE
frontend/                 React UI (Vite): Chat, Webhooks dashboard, notifications
mock_data/                cases.json, entitlements.csv, usage.json
postman/                  collection and environment
scripts/                  token minting, Postman collection generator
docs/                     MCP demo transcript
Dockerfile                one Python image for api / upstreams / mcp / chat / webhook-receiver (targets: runtime, debug)
docker-compose*.yml       full stack, and the debugpy overlay
.vscode/                  launch configurations and Docker tasks
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Chat answers *"Claude credentials are missing"* (503) | `ANTHROPIC_API_KEY` is not in the `chat` container. Put it in `.env` (not `.env.example`), then `docker compose up -d chat` (a plain `restart` keeps the old environment). Check `http://localhost:8100/health` → `claude_credentials: true` |
| Chat answers *"credit balance is too low"* | The key works but the Anthropic account has no credits: add them under Plans & Billing in the Anthropic Console |
| Browser: `ERR_CONNECTION_REFUSED` on a port | That container is not running: `docker compose ps`, then `docker compose logs <service>` |
| A container hangs while starting | Its host port is taken by another program (e.g. a Jupyter kernel on 9000). Find it with `Get-NetTCPConnection -LocalPort <port>` (PowerShell) and change the published port |
| Docker commands themselves hang | Restart Docker Desktop, then `docker compose up -d --wait` |
| UI still shows old behaviour | Hard refresh (Ctrl+Shift+R): the browser cached the previous bundle |
| `403 … requires the 'webhooks:manage' scope` | Use the `platform_admin` persona for webhook and audit endpoints |
| Escalation returns 412 | The `If-Match` ETag is stale: re-read the case (`GET /v1/cases/{id}`) and use its new `ETag` |
| Webhooks tab: *receiver not reachable* / bell dot grey | The `webhook-receiver` container is down, or the page was built with a different `VITE_RECEIVER_URL` |

## What was verified

| Check | Result |
|---|---|
| `python -m pytest` | 114 passed |
| `npm run build` (frontend type-check and build) | passed |
| `docker compose up --build` | all 6 services healthy. Checked across services: chat → api dev token, api → upstreams (and fault injection), MCP HTTP `get_customer_summary`, chat → Claude → MCP → api (answered the ACC-007 summary), CORS from :5173 to api and receiver |
| `docker-compose.debug.yml` | services healthy, debugpy ports open (checked for 5678–5681) |
| Webhook scenarios against the live stack | healthy (delivered, 1 attempt), `fail_next` (500, 500, 200), down → manual retry (delivered on attempt 4), `reject` (1 attempt, no retry), `slow` (3 timeouts; receiver: 1 accepted + 2 duplicates); SSE stream delivered the escalation event to a listening client |
| Live server (`uvicorn`) + every Postman request replayed in order | 32/32 returned the expected status (recorded when the collection was built) |
| `python -m mcp_server.demo` (real stdio subprocess) | answered the sample question with CS-10231, CS-10234, CS-10238 |
| `newman` (Postman's own runner) | **not run**; the collection was replayed with an equivalent Python script instead |
| `python -m mcp_server.demo --llm` | **not run** |
| The React pages in a real browser | **not driven by an automated test**; the dashboard's API calls, CORS and the built bundle were checked |

## Further reading

[DECISIONS.md](DECISIONS.md) covers trade-offs, assumptions and skipped work. [SECURITY.md](SECURITY.md) covers
governance and guardrails. [TESTEVALS.md](TESTEVALS.md) lists the MCP evals. [AI_USAGE.md](AI_USAGE.md) records how
AI assistance was used and checked.
