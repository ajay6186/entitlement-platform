# Decisions, trade-offs and assumptions

Each entry gives the decision, why, its failure mode, and what would change at scale.

## API design

**Contract first, hand-written OpenAPI 3.1 as the source of truth.** The app serves `openapi.yaml` at `/docs`
rather than generating one. Contract tests check that routes and spec match exactly and that real responses,
including errors, validate against the documented schemas. *Failure mode:* the spec and code drift. The parity
test fails the build when that happens. *At scale:* lint the spec in CI (Spectral) and add property-based contract
fuzzing (Schemathesis).

**REST, not GraphQL or gRPC.** The workflows are stable and resource-shaped (customer, case, comment,
escalation), and REST gives HTTP caching, ETags, idempotency keys and status codes that gateways and Postman
understand. GraphQL would help if many clients needed differently shaped aggregates. gRPC suits
high-throughput service-to-service calls (for example between this platform and the usage warehouse) but not
external partners or browsers.

**Versioning:** major version in the path (`/v1`). Additive changes ship in place. Breaking changes go to `/v2`,
with `Deprecation` and `Sunset` headers on `/v1` and at least six months of overlap. Clients must ignore unknown
fields.

**Errors:** RFC 9457 `application/problem+json` everywhere, with stable `type` slugs (for example `case-closed`
and `idempotency-key-reused`) so clients branch on the type, not the message. Every error carries a
`correlation_id`.

**Search semantics.** Filters AND together, and a repeated parameter ORs its values. Unknown query parameters
are rejected with 422, because a silently ignored typo (`prioirty=P1`) widens the result set, which is dangerous
for agents. Sorting is `opened_at` with `id` as the tie-break, so it is fully deterministic. Pagination is
**keyset-based** (opaque cursor holding the last `(opened_at, id)`): it stays stable while cases are inserted,
which offset paging does not. The cursor also embeds a hash of the query, so it cannot be replayed with different
filters (400 `invalid-cursor`).

**`support_tier` is filtered in the platform.** Tier lives in the entitlement system, so the platform fetches the
matching cases, then batch-loads their entitlements in one call (no N+1), then filters.
*At scale:* the platform would hold a small replicated `account -> tier` read model, kept fresh by entitlement
change events, and push the filter down to the case-system query. Today, filtering in memory means every page
reads the full filtered case set from upstream.

**`days_until_renewal` and the MCP `age_hours` are computed server-side** so an LLM never does date arithmetic,
which is a common source of wrong answers.

## Backend

**Layers:** routes (HTTP only), services (rules), clients (one per upstream, mapping its format into platform
domain models). The mock upstreams deliberately use different shapes (camelCase, CSV strings), so the mapping is
real.

**Upstreams over real HTTP.** The mocks are a separate ASGI app, reached through `httpx`'s ASGI transport. Clients
exercise real status codes, headers, timeouts and retries with no extra process. Setting
`PLATFORM_UPSTREAM_BASE_URL` switches to the network; Docker runs the mocks as their own `upstreams` service so the
API really crosses a network boundary. The dev endpoints `/dev/upstream-faults` and `/dev/reset` work in both
modes: with a remote mock they forward to its `/_admin/*` endpoints.

**Resilience:**
- Every call is bounded by a timeout: `asyncio.wait_for`, because ASGI transports ignore socket timeouts.
- Only transient failures are retried (transport errors, timeouts, 429/502/503/504), with exponential backoff and
  jitter, up to 3 attempts.
- **Retries are only for calls that are safe to repeat.** Comment creation is retried because the platform
  forwards an idempotency key upstream. Escalation is *not* retried: a retry after a write that was applied but not
  acknowledged would look like a version conflict.
- Each upstream has its own circuit breaker (5 consecutive failures opens it for 30 s, then half-open). Breaker
  state appears in `/healthz`.
- When usage is down, the customer endpoint returns 200 with `degraded: true`. When entitlement is down it returns
  503, because a customer view without entitlement is not meaningful.

*At scale:* breaker and limiter state move to a shared store or service mesh. Today they are per process.

**Idempotency.** Keys are scoped per caller (`sub`) and stored with a SHA-256 fingerprint of method, path and body,
and the first response. Replays return the stored response with `Idempotent-Replayed: true`. Reusing a key with a
different body returns 422. A key still in flight returns 409; the unique constraint on `(client_id, key)` acts as
the lock. If the first attempt fails, the record is deleted so the client can retry with the same key. Records
expire after 24 h.

**Optimistic concurrency.** The case `version` is exposed as an `ETag`. `If-Match` on escalate returns 412 when it
is stale. The platform's write to the case system is always conditional on the version it read, returning 409
`concurrent-modification` on a race. `If-Match` is optional so simple clients still work; the MCP server always
sends it.

**Audit and events in one transaction (transactional outbox).** A successful escalation writes the audit row,
the `domain_events` row and one pending `webhook_deliveries` row per subscription in a single commit. Delivery
happens after the response. Denied and failed attempts are audited too.
Comments publish `case.comment_added` the same way, committed together with the idempotency record, so an
idempotent replay never publishes a second event.
*Recovery:* deliveries left `pending` (process stopped between commit and send) are resumed at start-up, and
failed deliveries can be re-sent from the dashboard (`POST /v1/webhooks/deliveries/{id}/retry`). *Remaining
gap:* there is no periodic sweeper while the process runs, so a delivery stuck `pending` waits for the next start.
At scale a separate worker reads the outbox. The upstream case write and the local commit are also not atomic;
at scale that becomes a saga, or change-data-capture events from the case system.

**Webhooks:** signed in the Standard-Webhooks style, HMAC-SHA256 over `id.timestamp.body`. Delivery is
at-least-once, with up to 3 attempts and exponential backoff; permanent 4xx responses (other than 408 and 429) are
not retried. Every HTTP attempt is recorded (`webhook_attempts`: signature headers sent, status or error, response
excerpt, duration) so the dashboard can show a delivery timeline. Receivers check the signature in constant time,
reject timestamps outside ±300 s and de-duplicate on the event id. Subscriptions store a *reference* to the
secret, never the secret.

- **Event payloads carry ids, not content.** `case.comment_added` sends `case_id`, `comment_id` and `author_id`
  but not the comment text: customer-written text stays in the case system, and receivers fetch it with their own
  authorization if they need it.
- **The demo receiver is a separate service with its own verification code** (`webhook_receiver/`), not a call into
  the platform's helpers, so it demonstrates what a third party has to implement. The API's built-in
  `/v1/webhooks/receiver` remains as the reference receiver used by the tests.
- **Failure simulation lives in the receiver** (`PUT /control`: fail, fail_next, slow, reject) so retries,
  permanent errors, timeouts and duplicates can be shown live without touching the sender.

## Deployment, UI and notifications

**One Python image, several services.** `api`, `upstreams`, `mcp`, `chat` and `webhook-receiver` share one image
and differ only in their command, which keeps builds fast and versions in step. A `debug` build target adds
debugpy for the VS Code attach configurations. Health checks gate start-up order. *At scale:* separate images per
service so each can be released and scaled on its own.

**Host port 9100 for the mock upstreams.** Jupyter/VS Code kernels commonly hold ports 9000–9004 on Windows, which
made the container hang while starting. Inside the compose network the service is still `upstreams:9000`.

**The dashboard calls the API directly from the browser** (CORS allowlist, bearer token, no cookies) instead of
proxying through the chat backend. It uses its own `platform_admin` token, separate from the chat's sign-in, so a
demo can escalate as `support_lead` while watching deliveries with admin rights.

**Notifications use Server-Sent Events from the receiver**, not WebSockets or polling. The flow is one-way
(receiver → browser), SSE reconnects automatically and needs no extra library, and the pop-up appears the moment
the webhook is processed. The dashboard tables themselves refresh every 3 s, which is simple and good enough for a
local demo. *At scale:* an authenticated event stream (or WebSockets behind a gateway) with per-user filtering.

**Chat replies are Markdown with a fixed layout** (answer, table, needs attention, flagged content, next step),
rendered with `react-markdown`, which does not render raw HTML, because replies quote untrusted case text.

**Storage:** SQLite through async SQLAlchemy 2. The schema is PostgreSQL-compatible: there are unique constraints
and indexes on idempotency `(client_id, key)` and `expires_at`, audit `(resource_type, resource_id, occurred_at)`
and `(actor_id, occurred_at)`, and deliveries `(status, next_attempt_at)`. `create_all` keeps startup to one
command. A real deployment would use Alembic migrations.

**Two clocks.** "Business now" (case age, renewal windows) can be pinned with `PLATFORM_FIXED_NOW`, so the demo
and evals give the same answer on any day. Wall-clock time is used for tokens, webhook timestamps and audit. This
is why `escalated_at` in the demo shows real time.

## MCP

- **The MCP server is an adapter, not a second backend.** It calls the REST API with the user's token, so
  authorization, redaction and business rules cannot diverge between REST and agent channels.
- **FastMCP with a strict subclass.** Tools are declared with `@mcp.tool()` and typed parameters (`Literal` enums,
  `Field` patterns and bounds). FastMCP by default ignores unknown arguments and skips JSON-Schema validation, so
  `StrictFastMCP` adds `additionalProperties: false` to every schema and turns on input validation: a model that
  invents a parameter gets an error instead of a silently different query.
- **Concise, LLM-shaped outputs.** Computed age and days to renewal, explicit `data_notes` for degraded or
  redacted data, at most 25 cases, text truncated to 300 characters, and a 12 KB cap on each response.
- **Escalation confirmation** uses MCP elicitation when the client supports it, so the human answers in the client
  UI. Otherwise it falls back to a signed, single-use, 5-minute confirmation token bound to user, case, reason and
  case version. The tool is annotated `destructiveHint: true` so hosts can require approval. See SECURITY.md for
  the limits of the token fallback.
- **The comment tool derives its idempotency key** from (user, case, text), so an agent repeating a call cannot
  double-post. Trade-off: posting the identical comment twice on purpose within 24 h is treated as a retry.
- **The package is named `mcp_server/`, not `mcp/`** as the suggested layout had it: a local `mcp/` directory
  would shadow the `mcp` SDK package on import.

## Assumptions

- The brief's CSV example uses `ACC-77` while its case and usage examples use `ACC-007`. The fixtures standardise
  on `ACC-007`.
- "Open" means status `open`. `new` and `pending` are not open for the sample question.
- "Renewal in the next 90 days" means `0 <= days_until_renewal <= 90`, inclusive.
- Escalation sets the case's escalated flag and reason. It does not change priority.

## Skipped, and how it would be finished

| Item | Next step |
|---|---|
| Real IdP (OIDC, RS256, JWKS) | Replace `decode_token` with JWKS validation; the `/dev/token` endpoint disappears (it is already disabled by a flag) |
| Resource-level authorization (for example, agents see only their region's accounts) | Add an account-assignment claim or lookup and enforce it in services; today authorization is by scope plus field-level ARR rules |
| Periodic outbox sweeper | Start-up resumption and manual retry are done; a background task scanning `status='pending' AND next_attempt_at < now` while running is not |
| Authentication on the demo receiver's `/control`, `/events` and `/events/stream` | Fine for a local demo; a real receiver would authenticate the dashboard and filter the stream per user |
| Postman coverage of the webhook dashboard endpoints | Add them to `scripts/build_postman.py`; Swagger covers them today |
| Idempotency-Key on escalate | Escalation is already naturally idempotent (a second attempt returns 409); a key would make replays return 200 |
| List comments endpoint and comment PII scopes | `GET /v1/cases/{id}/comments` with a `cases:read_pii` scope that controls redaction |
| Stretch item (Kafka or Kubernetes) | Deliberately not done: the time went to the core, the tests and agent safety |
