# AI usage

## Tools used
- **Claude Code** (Claude Opus 5.5) as a coding assistant for scaffolding, implementation, tests and documentation
  drafts, working inside this repository with the ability to run commands.
- The optional `--llm` demo calls the Claude API (`claude-opus-5-5`) to drive the MCP tools.

## How outputs were verified
- **Tests are the gate:** 114 automated tests, including contract tests against `openapi.yaml` and MCP evals over a
  real MCP session. Each test was written against the brief's expected behaviour, and failures were investigated
  rather than suppressed.
- **Live run:** started `uvicorn`, replayed every Postman request in order against it (32/32 expected statuses),
  and confirmed the escalation webhook was delivered over real HTTP with a verified signature.
- **Demo:** ran `python -m mcp_server.demo` end to end over stdio. It returned the three expected cases.
- **Docker:** the full stack (6 services) was built and run; calls between services, the chat through Claude, and
  every webhook failure scenario were exercised against the running containers.
- **Library behaviour** (FastAPI routing, the MCP SDK's FastMCP and elicitation, in-memory sessions) was checked
  against the installed packages' source, not assumed.

**Not verified:**
- Postman's own `newman` runner (the collection was replayed with an equivalent script instead)
- the `--llm` demo
- the React pages in a real browser by an automated test (their API calls, CORS and the built bundle were checked)

## What the AI got wrong, and how it was caught
| Issue | How it was caught | Fix |
|---|---|---|
| MCP elicitation approval checked `confirm is True`; the SDK turns JSON `true` into `1.0`, so approvals were read as declines | Eval test failed (scenario 10) | String-enum decision; see TESTEVALS.md |
| Wrong expected sort order in a search test (CS-10235 opened before CS-10245) | Test failed; checked against the fixture dates | Corrected the expectation, not the code |
| Route-parity contract test assumed `app.routes` lists `APIRoute`s; FastAPI 0.139 wraps included routers, so the set was empty | Test failed | Version-tolerant route walk, plus an assertion that the set is non-empty |
| First OpenAPI draft documented the webhook as an operation callback keyed on a nonexistent `callback_url` | Review of the spec | Moved to OpenAPI 3.1's top-level `webhooks` section |
| Timestamps read back from SQLite are naive and were serialised without a timezone | Smoke test output | `UtcDatetime` type on response schemas |
| Phone-redaction regex as first drafted would also have matched ISO dates such as `2026-11-22` | Review | Tightened to phone-number shapes |
| The suggested `mcp/` folder name would shadow the `mcp` SDK package | Caught during design | Named the package `mcp_server/` |
| Chat errors raised inside the MCP session arrive wrapped in an `ExceptionGroup`, so a missing API key surfaced as a vague 502 instead of the intended 503 | Reproduced the failing request inside the container and unwrapped the group | Unwrap to the root cause before mapping errors to HTTP |
| React effect written as `useEffect(() => el.scrollIntoView(...))` returned a value; newer browsers return a Promise from `scrollIntoView`, which React then called as the cleanup ("n is not a function") | Browser stack trace mapped to React's effect-cleanup code in the bundle | Braces so the effect returns nothing |
| Moving the mock upstreams to their own container broke `/dev/upstream-faults` (409) | Review of the dev endpoints | Forward to the standalone service's `/_admin/*` |
| A container hung while starting: a Jupyter kernel held host port 9000 | Inspected the process that owned the port | Published the mock upstreams on 9100 |
| The first description of the receiver's `slow` mode said the sender would eventually succeed; in fact every attempt times out (delivery failed) while the receiver still processes the first request | Ran the scenario against the live stack | Corrected the UI hint, README and docstring |
| An earlier manual test guide claimed escalating without `If-Match` returns 428; it is optional and the escalation proceeds | Checked against openapi.yaml while updating the docs | Corrected the docs |
| New tests used 6-character idempotency keys; the API requires 8–255 | Tests failed with 400 | Fixed the tests, not the validation |
| Platform logs going to stdout would corrupt the MCP stdio protocol in embedded mode | Caught during design | `PLATFORM_LOG_STREAM=stderr` set by the MCP server |

## Judgement calls kept with the human
Scope choices (no Kafka or Kubernetes), the confirmation design and its documented limits, the assumptions in
DECISIONS.md and what to claim as verified were all reviewed rather than accepted as generated. No test result is
claimed here that was not actually observed.
