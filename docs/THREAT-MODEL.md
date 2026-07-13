# Threat Model

STRIDE pass over the system's trust boundaries. Written for two readers: a
reviewer deciding whether the HITL guarantees are real, and future-me
changing a boundary and needing to know what it was defending against.

## System & trust boundaries

```
                        ┌─ TB1: public internet ─────────────────────────┐
  Browser / API client ─┤  FastAPI app (auth, RBAC, rate limits, UI)     │
                        └───────┬────────────────────────────────────────┘
                                │ TB2: process/network boundary (stdio or
                                │      bearer-token HTTP, no published port)
                        ┌───────▼───────────┐   ┌─ TB4: outbound ────────┐
                        │  MCP gateway      │   │ LLM provider API        │
                        │  (approval gate,  │   │ (Groq/Anthropic/...)    │
                        │   rate limit, CB) │   └─────────────────────────┘
                        └───────┬───────────┘
                                │ TB3: database (Postgres/SQLite)
                        ┌───────▼───────────┐
                        │ App DB + LangGraph│
                        │ checkpoints, audit│
                        └───────────────────┘
```

Assets, in priority order: (1) the ability to execute sensitive identity
actions, (2) the audit trail's integrity, (3) employee PII in the mock
identity store, (4) credentials (API keys, reviewer tokens, LLM keys),
(5) LLM spend.

## TB1 — Internet → API

| STRIDE | Threat | Mitigation (where) |
|---|---|---|
| S | Caller pretends to be another integration | `X-API-Key` resolved to an `ApiClient` row; no shared-string compare (`app/api/auth.py`) |
| S | Caller decides approvals as another reviewer | Per-reviewer token or OIDC-verified JWT; reviewer NEVER read from request body (`require_reviewer`) |
| T | Forged Telegram webhook decides approvals | `X-Telegram-Bot-Api-Secret-Token` verified against configured secret |
| R | "I never approved that" | Approval rows record reviewer + auth method + IdP subject + timestamps; audit log per ticket |
| I | STANDARD client reads others' tickets/audit | Caller-scoped reads (`_client_may_see_ticket`) |
| I | /metrics leaks operational aggregates | Documented trade-off; block at ingress if sensitive (see `prometheus_metrics` docstring) |
| D | Request floods / LLM-spend abuse | slowapi rate limits on mutating endpoints; per-client daily caps; demo key capped at 10/day |
| E | Demo reviewer decides real approvals | Demo reviewer additionally confined to demo-owned tickets (`_authorize_demo_reviewer_scope`) |

## TB2 — Agent → MCP gateway

| STRIDE | Threat | Mitigation |
|---|---|---|
| S | Rogue network client calls tools directly | HTTP transport requires bearer token; port not published in compose; localhost-only sidecar in Helm |
| S | DNS-rebinding from a victim's browser | Host/Origin allowlists via the SDK's TransportSecurityMiddleware, ON (off is the SDK default) |
| T/E | **Prompt-injected planner calls a sensitive tool** | The core defense stack: sensitive tools require server-verified `approval_id` for the EXACT tool+args; ticket text wrapped as untrusted with guardrail prompt; plan schema/size/username validation; PII masking; injection-refusal case pinned in the golden-ticket evals |
| E | One approval reused for N executions | `executed_at` single-use marking in the approval gate |
| E | Approved args swapped at execution time | Gate matches on exact tool + arguments, mismatch refused (covered by `test_approval_target_mismatch.py`) |
| D | One backend domain down takes all calls with it | Per-domain circuit breakers + `/health` exposure + `mcp_circuit_breaker_open` metric/alert |

## TB3 — Database

| STRIDE | Threat | Mitigation |
|---|---|---|
| T | Schema drift / ad-hoc DDL | Alembic chain is the reviewed path; CI drift gate (`tests/test_migrations.py`) makes editing models without a migration fail |
| R | Audit rows silently duplicated by replicas | SLA sweep runs under a Postgres advisory lock — one pass per interval cluster-wide |
| I | Secrets in the DB (reviewer tokens, API keys) | Threat accepted at demo scope and documented: tokens are stored plaintext for inspectability; a real deployment should hash them (listed in SECURITY.md's roadmap) |
| D | Checkpointer loss orphans in-flight HITL runs | Postgres-backed checkpoints; stuck-ticket sweep fails orphans visibly instead of forever-spinners |

## TB4 — Outbound LLM calls

| STRIDE | Threat | Mitigation |
|---|---|---|
| I | Employee PII shipped to the LLM provider | `_mask_pii_for_prompt` masks emails/records before prompts; observation text is the masked form |
| D | Provider outage stalls tickets | Node-level retry policy (transient-only); failures land tickets in FAILED with reasons, not hangs |
| D (spend) | Replan loop burns tokens unboundedly | `MAX_REPLANS` + opt-in `MAX_TOKENS_PER_TICKET` hard cap + `llm_tokens_total` metrics and alert |

## Known gaps (accepted, with reasons)

1. **Reviewer/API tokens stored unhashed** — inspectability for a demo
   system; flagged in SECURITY.md. Hash-at-rest is the next hardening step
   if this ever holds real identities.
2. **MCP bearer token is static** — OAuth 2.1 per MCP spec is deliberately
   descoped (ROADMAP 4.3); network isolation is the compensating control.
3. **Rate limiter is per-process** — replicas multiply effective limits;
   documented in the chart and Dockerfile; a shared store (Redis) is the
   fix when it matters.
4. **SQLite mode has no advisory locks** — single-replica by definition;
   the lock no-ops honestly rather than pretending.
