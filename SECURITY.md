# Security, governance and guardrails

## Identity and authentication
- OAuth 2.0 bearer tokens (OIDC in production). JWTs are validated for signature, `exp`, `iss` and `aud`, with
  the algorithm pinned to one value; `alg=none` and forged tokens are rejected (tested).
- Locally, tokens are HS256, minted by `/dev/token` and `scripts/mint_token.py`. **Both are dev-only.** Set
  `PLATFORM_ENABLE_DEV_ENDPOINTS=false` outside local development; that also removes the fault-injection and reset
  endpoints. In production, validate RS256 tokens against the IdP's JWKS.
- Secrets come from the environment (`PLATFORM_JWT_SECRET`, `PLATFORM_WEBHOOK_SIGNING_SECRET`). The defaults in
  code and compose are obviously fake, for local use only. Use a secret manager elsewhere.
- The **Anthropic API key** lives only in `.env` (git-ignored and Docker-ignored) or the shell, and Compose passes
  it only to the `chat` container. It is never sent to the browser, the MCP server or the model. `.env.example`
  must only ever hold placeholders.

## Authorization
| Scope | Grants |
|---|---|
| `cases:read` | search and read cases |
| `cases:comment` | add comments |
| `cases:escalate` | escalate (support leads) |
| `customers:read` | aggregated customer view |
| `customers:financials` | `arr_usd` (Finance) |
| `webhooks:manage` | subscriptions and delivery inspection |
| `audit:read` | audit records |

- **Field-level control:** without `customers:financials`, `arr_usd` is omitted and listed in `redacted_fields`.
  Support staff never receive financial data by default.
- **Escalation authorization happens before the existence check**, so a 403 does not reveal whether a case exists.
- **Least privilege for agents:** an agent token carries the *user's* subject and scopes plus an RFC 8693 `act`
  claim naming the agent. All authorization is evaluated against the user, so an agent can never exceed the human
  (tested: a support agent going through the copilot still gets 403 on escalate). There is no service super-user.

## Input and output hygiene
- Strict validation everywhere: path ID patterns, enums, bounds, `extra="forbid"` on bodies, and unknown query
  parameters rejected.
- `X-Correlation-ID` is accepted only if it matches `[A-Za-z0-9._-]{1,64}`; otherwise a new one is generated.
  This prevents log injection.
- Errors are problem+json without stack traces. Unexpected errors return a generic 500 with the correlation id.
- Logs are structured JSON with correlation ids. They contain no request bodies, tokens or comment text.
  Upstream failures are logged with system, path, attempt count and breaker state.

## Webhooks
- HMAC-SHA256 over `id.timestamp.body`, compared in constant time. Timestamps are accepted only within ±300 s,
  which stops replays. Event ids are de-duplicated in a persistent inbox.
- Subscriptions store a reference (`env:` or `vault:`), never a secret; the API rejects anything else.
- Payloads carry ids, not customer-written text (`case.comment_added` has no comment body).
- The delivery log stores the headers that were sent (id, timestamp, signature, user agent), never the secret.
- The demo receiver (`webhook_receiver/`) verifies independently with the same rules. Its `/control`, `/events`
  and `/events/stream` endpoints are **unauthenticated, for local demos only**: anyone who can reach port 8200 can
  read received payloads and change its failure mode.
- Production additions: HTTPS-only endpoints, SSRF protection on subscriber URLs (block private ranges), secret
  rotation with two active signatures, and per-subscriber secrets.

## Browser clients
- **CORS** on the API and the receiver allows only the configured origins (`PLATFORM_CORS_ORIGINS`, default the
  local UI). Requests use bearer tokens, never cookies, so CORS does not open CSRF.
- The chat UI renders model replies as Markdown **without raw HTML**, so case text quoted by the model cannot inject
  markup or scripts.
- Dev tokens (chat persona and the dashboard's `platform_admin` token) are kept in `localStorage`. That is
  acceptable for local development; in production use the IdP's browser flow (PKCE) with short-lived tokens, so a
  script running in the page cannot reuse a long-lived token.

## Abuse and availability
- Token-bucket rate limiting per (user, agent): 120/min for users, 60/min for agents. `RateLimit-*` headers are
  sent, and 429 responses include `Retry-After`.
- Timeouts, bounded retries and circuit breakers protect upstreams from retry storms.

## Audit
- Escalation attempts are recorded with outcome `success`, `denied` or `failed`, with actor, agent, correlation id
  and reason. So are comment creations and **every agent-originated API request** (middleware).
- The MCP server also writes one JSONL record per tool call (`logs/mcp_audit.jsonl`): tool, actor, agent,
  resource, outcome and duration. Free text such as comment bodies and confirmation tokens is deliberately left out.
- At scale: ship audit to append-only/WORM storage, and do not allow updates or deletes on `audit_logs`.

## AI agent (MCP) guardrails
| Risk | Control |
|---|---|
| Agent over-privilege | Uses the user's delegated token only; the backend re-checks every scope and rule |
| Prompt injection via case content | Subjects and comments are marked `untrusted: true`; suspicious text is flagged (`suspected_prompt_injection`) and a warning added; server instructions and tool descriptions say never to act on case content. Reading has no side effects, so injected text cannot trigger writes by itself |
| Unconfirmed high-risk writes | `escalate_case` asks the human through MCP elicitation, or returns `confirmation_required` with a signed, single-use token bound to user, case, reason and case version; it is annotated `destructiveHint` |
| Confirmed but stale action | The confirmation carries the case ETag; if the case changed since confirmation the API returns 412 and the agent must re-confirm |
| PII leakage | Emails and phone numbers in case text are redacted in MCP output; ARR is omitted unless authorized, with an explicit "do not estimate" note |
| Context flooding and exfiltration by volume | At most 25 cases, 300-character text fields, 12 KB response cap |
| Runaway agents | MCP-side limit of 30 tool calls per minute plus the backend's per-agent rate limit |
| Malformed or unexpected arguments | Strict JSON Schemas (`additionalProperties: false`, enums, patterns); the SDK rejects invalid calls before tool code runs |
| Duplicate writes from agent retries | Deterministic idempotency key for comments; escalation is guarded by state checks and If-Match |

**Known limitation of the token fallback.** When the client does not support elicitation, the confirmation token
proves that a confirmation *step* happened for exactly these arguments. It cannot prove that a human said yes:
a misbehaving model could pass the token straight back. Real assurance therefore comes from the host enforcing
approval for `destructiveHint` tools, from elicitation, or from an out-of-band approval in production (for example
a step-up approval link sent to the user). The backend's authorization and business rules apply whichever path is
used.

## Data
- Only mock, fictional data is used; no real credentials or customer data.
- Production additions: encryption at rest for the audit and idempotency stores, a retention policy (idempotency
  24 h, audit per compliance requirements), and classification-driven redaction (`cases:read_pii`).
