# Roadmap — Expanding to a Real, Deployable, End-to-End System

This was originally a forward-looking plan, researched and synthesized on
2026-07-03. **It has since been implemented** — see the per-stage status
notes below and `README.md` for the resulting architecture/feature list.

Grounded in an actual audit of this codebase (file/line citations throughout)
plus current research across four areas: LangGraph orchestration patterns,
MCP multi-server topology, production deployment/observability tooling, and
enterprise identity/reliability patterns.

## Enterprise-hardening pass (2026-07-13)

Beyond the staged roadmap below, one reviewed sweep took the system to
enterprise grade — every item gated by the (now 417-test) suite, a
mypy-clean `app/`, and an 80% coverage floor:

- **Metrics + alerting**: Prometheus `/metrics` (RED + domain series:
  approvals pending/decided/escalated, circuit-breaker state, LLM tokens,
  tickets by terminal status), a compose overlay shipping
  Prometheus/Alertmanager/Grafana with a provisioned dashboard, and seven
  runbook-annotated alert rules (`observability/`, `docs/RUNBOOKS.md`).
- **OIDC reviewer identity (4.1, right-sized)**: IdP-verified bearer JWTs
  on the approval endpoints — JWKS-via-discovery, issuer/audience/expiry,
  RS256 pinned — with per-decision provenance columns
  (`reviewer_auth_method`, `reviewer_oidc_subject`); legacy reviewer tokens
  keep working; entirely off unless configured (`app/api/oidc.py`).
- **Versioned migrations**: Alembic chain + CI drift gate (a model edit
  without a migration fails `tests/test_migrations.py`) + a parity test
  proving `alembic upgrade head` ≡ `init_db()`; Kubernetes applies
  migrations via a pre-upgrade Job.
- **Multi-replica safety**: the SLA sweep runs under a Postgres advisory
  lock — one pass per interval cluster-wide, no duplicate escalation audit
  rows (no-op on SQLite); gunicorn `--graceful-timeout 90` drains long
  agent runs on redeploy.
- **Cost control**: opt-in `MAX_TOKENS_PER_TICKET` — a checkpointed
  per-ticket token budget surviving HITL resumes, with metric + alert
  (`app/agent/token_budget.py`).
- **Golden-ticket evals**: six pinned tickets (all categories,
  re-onboarding, no-action, prompt-injection refusal) replayed through the
  real graph in CI; `python -m evals.run_live` holds the live model to the
  same contract. Authoring this immediately caught a real bug: the local
  `.env`'s stale `SENSITIVE_ACTIONS` silently exempted `enable_user` from
  human approval. Its first live run then drove a config change: the 8b
  Groq default measured 3/6 (hallucinated tool name, category misses —
  safety gates all held) vs 5/6 for `llama-3.3-70b-versatile`, which
  `.env`/`render.yaml` now pin.
- **Supply chain**: mypy + strict pip-audit + coverage gate in CI, Trivy
  scan gating every image push, Dependabot (grouped weekly), tag-driven
  release workflow (`release.yml`).
- **Packaging/ops**: Helm chart (probes, non-root + dropped capabilities,
  localhost-only MCP sidecar, migration hook, `helm lint`-clean),
  `scripts/validate_postgres.sh`, `SECURITY.md`, `docs/THREAT-MODEL.md`,
  `docs/RUNBOOKS.md`.

**2.2 status update**: the Postgres path (app DB + `AsyncPostgresSaver`)
remains blocked from LIVE validation on the dev machine (Docker Desktop
issue), but the full proof is now scripted — run
`GROQ_API_KEY=... ./scripts/validate_postgres.sh` on any Docker-capable
host: it validates checkpoint durability across a hard app restart,
approval-resume after that restart, a fresh-database `alembic upgrade
head`, and the metrics endpoint, then tears itself down.

---

## Implementation status (as of 2026-07-03)

- **Stage 1 (Orchestration Depth & Reliability Primitives) — DONE, all 6
  items.** Retry/backoff, idempotency keys, `Send`-based fan-out, dynamic
  replanning, MCP session reuse (owner-task/queue-proxy pattern, after a
  live anyio cross-task bug forced a redesign), rate limiting.
- **Stage 2 (Multi-Server MCP Topology & Resilience) — DONE except 2.2.**
  Domain server split (identity/access/ticketing, composed into one gateway
  via `add_tool()` — `FastMCP.mount()` doesn't exist in the installed SDK,
  so this achieves the same outcome a different way), config-driven server
  registry, per-domain circuit breaker + health check, supervisor/router
  classifier. **2.2 (Postgres migration) is BLOCKED** on a local Docker
  Desktop credential-helper issue outside this codebase's control — the app
  DB path is written to be database-agnostic already, but hasn't been
  validated against a live Postgres container, and the LangGraph
  checkpointer hasn't been migrated from `AsyncSqliteSaver` to
  `AsyncPostgresSaver`.
- **Stage 3 (Production Deployment Path) — DONE, all 5 items.** Dockerfile +
  docker-compose (written and YAML-validated; the Postgres-backed `app`
  service is unvalidated pending the same Docker blocker as 2.2), GitHub
  Actions CI (ruff + pytest) and a separate build-and-push-to-GHCR workflow,
  a real `/ready` probe + structured JSON logging with request correlation
  IDs, OpenTelemetry instrumentation for graph nodes/LLM calls/tool calls
  (live-verified), and secrets-manager guidance (docs only, since
  `pydantic-settings` already reads from the environment regardless of
  injection method).
- **Stage 4 (Real Identity & Enterprise Integration) — SCOPED DOWN, by
  design.** 4.1 (OIDC/Keycloak), 4.3 (MCP OAuth 2.1), and 4.4 (SCIM/OpenLDAP
  identity sync) are **intentionally skipped** — the roadmap's own trap
  notes call these the most legitimate items to scope down for a solo
  project, and none of them changes the core agent/MCP architecture being
  demonstrated. 4.2 (role/relationship-based approval authorization) and 4.5
  (SLA timeout + escalation) are implemented in a lightweight form:
  `app/api/rbac.py` layers an `it_admin`/`manager` role check on top of the
  existing free-text reviewer field (rather than real OIDC-verified
  identity), and `app/agent/sla_sweep.py` runs a plain `asyncio` background
  loop (not APScheduler) that escalates overdue approvals and flags stuck
  tickets. See README.md's "Identity & approval authorization" section for
  details.

---

## Stage 0 baseline (what exists today, for contrast — not a stage to build)

Single linear LangGraph DAG, one MCP server with 6 tools, two local SQLite
files (app DB + checkpointer), one shared static API key, no
containerization, no CI, no retries, no idempotency. This roadmap moves from
that baseline outward. Nothing below is implemented yet.

---

## Stage 1 — Orchestration Depth & Reliability Primitives

**Theme:** make the existing single-server, single-DB system behave like a
production agent — retries, idempotency, parallelism, dynamic replanning —
without yet touching infra (containers/Postgres/CI). This is the cheapest
stage to demo because it's all in-process code changes against the current
SQLite/single-MCP-server setup.

### 1.1 Retry/backoff on LLM and MCP calls
- **Build:** Wrap the two `llm.ainvoke()` call sites (`app/agent/graph.py:109,149`)
  and the `call_tool()` invocations (`app/agent/graph.py:132,262`, via
  `app/agent/mcp_client.py`) with either `tenacity`
  (`wait_random_exponential` + `stop_after_attempt(3-5)`, retrying only on
  connection errors/429/5xx, never on validation errors) or a LangGraph
  node-level `RetryPolicy` (built into `langgraph.types`, default
  `initial_interval=0.5s, backoff_factor=2.0, jitter=True`). For MCP
  specifically, retry the whole `mcp_session()` + `call_tool()` unit, not
  just the call — a poisoned session can't be reused after a transient
  failure.
- **Why it matters:** This is the #1 "what happens when things fail"
  interview question for any agent system, and right now the honest answer
  is "nothing, it just fails the ticket." Being able to say "I distinguish
  transient vs. permanent failures and retry with jittered backoff at the
  node level" is a concrete, demonstrable reliability story.
- **Scope:** Small.
- **Files:** `app/agent/graph.py`, `app/agent/llm.py`, `app/agent/mcp_client.py`;
  new dependency `tenacity` in `pyproject.toml`.

### 1.2 Idempotency key on `POST /tickets`
- **Build:** Accept a client-generated `Idempotency-Key` header on
  `POST /tickets` (`app/api/main.py:66-83`), store it in a new
  `idempotency_keys(key, request_hash, response_json, created_at)` table,
  and replay the first response verbatim on repeat keys instead of
  re-running the graph. TTL-expire after ~24h.
- **Why it matters:** Directly closes a named gap — right now resubmitting
  an identical payload creates a duplicate `Ticket` row and a duplicate
  graph run. This is the Stripe idempotency pattern, a well-known and
  interview-recognizable design (dedup table, hash the request, replay-on-repeat).
- **Scope:** Small.
- **Files:** `app/api/main.py`, `app/db/models.py` (new table), one new
  Alembic-equivalent migration or `Base.metadata.create_all` addition.

### 1.3 Parallel fan-out for independent steps (`Send` API)
- **Build:** Replace the sequential `plan_index` walk in `execute_step_node`
  (`app/agent/graph.py:251-273`) with a fan-out node that emits one
  `langgraph.types.Send(node, payload)` per independent step (e.g., granting
  5 resources in one onboarding ticket becomes 1 concurrent superstep
  instead of 5 sequential MCP round-trips), merging results via an
  `Annotated[list, operator.add]` reducer. Each parallel branch needs its
  own try/except returning a status rather than raising, since a LangGraph
  superstep fails atomically if any branch raises.
- **Why it matters:** This is the most visually demoable orchestration
  upgrade — a before/after latency comparison ("5 sequential grants: 12s →
  3 parallel grants: 3s") is a concrete number for a resume bullet or live
  demo, and "I used the Send API for fan-out/fan-in with a custom reducer"
  is a specific, non-generic LangGraph story.
- **Scope:** Medium.
- **Files:** `app/agent/graph.py` (new fan-out node, state schema change),
  `tests/test_graph_routing.py`.

### 1.4 Dynamic replanning after step results contradict assumptions
- **Build:** Insert a `replan_node` after `execute_step` that re-invokes the
  planner with accumulated step results, triggered only when a step's
  result contradicts a plan assumption (e.g., `get_user` reveals the target
  is already disabled, or a grant fails because the resource doesn't
  exist). This upgrades the current fixed-upfront-plan (ReWOO-style) into a
  plan-and-execute loop.
- **Why it matters:** Directly answers "what if the world doesn't match your
  plan?" — today the LLM handles this only via a soft prompt instruction
  not to redo already-done work (`PLANNER_SYSTEM_PROMPT`, `graph.py:56-72`),
  which is a suggestion, not a guarantee. A real replan node is a hard
  architectural answer.
- **Scope:** Medium.
- **Files:** `app/agent/graph.py` (new node + new conditional edge from
  `execute_step_node`).

### 1.5 Session/connection reuse across a plan's steps
- **Build:** Cache one MCP session per graph run (keyed by server name)
  instead of opening a fresh `mcp_session()` — and on stdio, a fresh
  subprocess — per individual tool call (`graph.py:131,261`).
- **Why it matters:** Cheap fix, real cost/latency story ("cut per-ticket
  MCP overhead by reusing sessions"), and becomes load-bearing once Stage 2
  adds multiple MCP servers (3 servers × per-call subprocess spawn is the
  kind of thing that gets flagged in a system-design interview).
- **Scope:** Small.
- **Files:** `app/agent/mcp_client.py`, `app/agent/graph.py`.

### 1.6 Rate limiting on mutating endpoints
- **Build:** `slowapi` on `POST /tickets` and `POST /approvals/{id}/decide`
  to bound blast radius from retry loops or automation feeding the planner.
- **Why it matters:** Small, standard, table-stakes hardening; shows
  awareness that an LLM-driven endpoint is a cost/abuse surface, not just a
  correctness surface.
- **Scope:** Small.
- **Files:** `app/api/main.py`.

**Stage 1 demo:** submit a 5-step onboarding ticket and show it execute in
parallel with sub-second retries recovering from an injected transient MCP
failure, plus a duplicate-submission returning the cached result instead of
double-provisioning.

---

## Stage 2 — Multi-MCP Topology & Postgres-Backed State

**Theme:** move from "one server, one SQLite file" to "multiple
domain-scoped MCP servers behind a router, one shared Postgres backing both
app state and checkpoints." This is the stage that turns the mock
`EmployeeUser` table into a believable multi-backend-system simulation and
removes the single-writer/single-machine ceiling.

### 2.1 Split the monolithic MCP server into domain servers, composed via FastMCP mounting
- **Build:** Split `app/mcp_server/tools.py` (163 lines, 6 tools) into three
  `FastMCP` instances — `identity_server.py` (`get_user`, `create_user`,
  `disable_user`), `access_server.py` (`grant_access`, `revoke_access`), and
  a new `ticketing_server.py` (simulating a Jira/ServiceNow-style
  ticket-sync tool) — then compose them under one gateway server using
  FastMCP's native `mcp.mount(server, namespace=...)`, which auto-prefixes
  tools (`identity_get_user`, `access_grant_access`) and requires no manual
  registry for this in-process case.
- **Why it matters:** This is the single most direct fix for the audit's
  "everything folded into one mock table" finding, and it's the cheapest
  possible step toward "multiple backend systems" — no new infra, just code
  reorganization plus one `mount()` call. `mcp_client.py`'s single
  `mcp_session()` keeps working unchanged since routing is invisible to the
  agent graph. Directly maps to the "identity/access/ticketing as separate
  systems" story that's obviously true of real IT environments.
- **Scope:** Medium.
- **Files:** new `app/mcp_server/identity_server.py`, `access_server.py`,
  `ticketing_server.py`; `app/mcp_server/server.py` becomes the mount-based
  gateway; `app/mcp_server/tools.py` logic redistributed.

### 2.2 Postgres for both the app DB and the LangGraph checkpointer (the actual scaling blocker)
- **Build:** Swap `sqlite+aiosqlite://` for `postgresql+asyncpg://` in
  `DATABASE_URL` — `asyncpg` is already declared but unused in
  `pyproject.toml:26`, so this is mostly a connection-string change plus
  migrations. Separately, install `langgraph-checkpoint-postgres` and swap
  `AsyncSqliteSaver` for `AsyncPostgresSaver.from_conn_string(...)` in
  `app/agent/runner.py:10,29`, calling `.setup()` once to create its
  tables. This is the single reconciled recommendation across the
  orchestration and deployment research — both flagged the same root cause
  (SQLite file-locking serializes writers; two local `.db` files tie the
  whole app to one machine) and the same fix.
- **Why it matters:** This is the one item that actually removes the
  horizontal-scaling ceiling described in the audit. Everything else in
  this stage is organization; this is the thing that makes "run 2 replicas
  behind a load balancer" possible at all. It's also a very standard,
  well-understood migration to narrate in an interview — no exotic tooling.
- **Scope:** Medium (mostly config + one migration script + testing
  WAL-adjacent edge cases go away).
- **Files:** `app/config.py`, `app/db/session.py`, `app/agent/runner.py`,
  `pyproject.toml`, new `alembic/` migration setup (Alembic isn't in the
  codebase yet — add it here rather than hand-rolling schema creation).

### 2.3 Minimum-viable server registry (config-driven, not a gateway product)
- **Build:** Extend `config.py:29`'s single `MCP_TRANSPORT` into a small
  `registry.yaml` or config table of `{name, url, transport}` entries, one
  per domain server, that `mcp_client.py` reads to pick the right session
  per tool call. This is explicitly the *lightweight* version — see the
  "trap to avoid" note below on IBM mcp-context-forge.
- **Why it matters:** Lets you talk about server discovery/routing without
  adopting an external gateway product disproportionate to a portfolio
  project. Also the natural place to add the health-check-per-backend
  improvement to `GET /health`.
- **Scope:** Small–Medium.
- **Files:** new `app/mcp_server/registry.py` or `registry.yaml`,
  `app/agent/mcp_client.py`, `app/api/main.py` (health endpoint).

### 2.4 Circuit breaker + per-backend health check
- **Build:** Wrap `mcp_session()` with a per-server breaker (hand-rolled
  three-state Closed → Open → Half-Open, or the `purgatory` library) plus
  timeout, and extend `GET /health` (`main.py:56-58`) to report
  per-backend-server status instead of a static `{"status": "ok"}`. Note
  this is explicitly *not* an MCP spec primitive — it's left to the
  orchestrator/gateway by design, so building it yourself is the correct
  move, not a workaround.
- **Why it matters:** Directly answers the audit's "no health check or
  circuit breaker exists for the MCP server" finding, and having 3 servers
  instead of 1 makes per-backend status genuinely necessary rather than
  cosmetic.
- **Scope:** Small–Medium.
- **Files:** `app/agent/mcp_client.py`, `app/api/main.py`.

### 2.5 Supervisor/router split for onboarding vs. offboarding vs. access-change (optional stretch in this stage)
- **Build:** Replace the single generic `PLANNER_SYSTEM_PROMPT`
  (`graph.py:39`) with a thin classifier that routes `Ticket.category` to
  one of three compiled subgraphs, each with its own tuned planner prompt,
  using the `langgraph-supervisor` package's `create_handoff_tool()`
  pattern.
- **Why it matters:** "One prompt handles onboarding, offboarding, and
  access changes alike" is a named limitation in the audit; specialized
  subgraphs is the textbook multi-agent-handoff answer and gives a
  legitimate "supervisor pattern" story for interviews. Marked
  optional/stretch here because it's a genuine scope/prompt-engineering
  investment, not because it's low-value — sequence it after 2.1–2.2 land
  since it's easiest to build once the domains are already separated into
  servers.
- **Scope:** Large.
- **Files:** `app/agent/graph.py` (major restructure), new
  `app/agent/prompts/` per-domain prompt modules.

**Stage 2 demo:** show the same onboarding ticket now dispatching tool calls
across three distinct MCP server processes with a live per-backend health
panel, and show two app instances pointed at one Postgres both handling
tickets concurrently without lock contention.

---

## Stage 3 — Real Deployment & Ops

**Theme:** ship it like a real service — containers, CI/CD, observability,
secrets management. This stage is intentionally infra-only; it assumes
Stage 2's Postgres migration already happened, since containerizing
SQLite-backed multi-instance state would just containerize the bug.

### 3.1 Dockerfile + docker-compose
- **Build:** Multi-stage Dockerfile — builder stage installs deps, runtime
  stage copies only the venv + app code onto a slim Python base. Run via
  Gunicorn managing Uvicorn workers (`gunicorn -k uvicorn.workers.UvicornWorker`)
  instead of raw `uvicorn --reload` (a dev-only flag currently in the
  README's only run instructions, `README.md:104-107`). `docker-compose.yml`
  gets `app`, `postgres` (with healthcheck + `depends_on: condition:
  service_healthy`), and `mcp-server` as its own container(s) when run over
  HTTP transport.
- **Why it matters:** Baseline table stakes for "deployable, end-to-end
  system" — currently zero containerization exists. This is also the
  fastest, most visually obvious "before/after" for the portfolio
  (`docker compose up` and it works).
- **Scope:** Medium.
- **Files:** new `Dockerfile`, `docker-compose.yml`, `.dockerignore`.

### 3.2 Minimal GitHub Actions CI/CD
- **Build:** PR workflow: checkout → setup-python (cached) → lint (`ruff`)
  → typecheck (`mypy`) → `pytest`. Main-branch workflow adds Docker Buildx +
  GHCR login (using the built-in `GITHUB_TOKEN`, no PAT needed) +
  build-push with `cache-from/to: type=gha`.
- **Why it matters:** "No `.github/workflows` exist" is a named gap; this is
  cheap, standard, and directly demoable via a green check badge on the
  README.
- **Scope:** Small.
- **Files:** new `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`.

### 3.3 Readiness probe + structured logging
- **Build:** Add `GET /ready` distinct from the existing static `GET
  /health` (`main.py:56-58`) — checks DB connectivity, MCP backend
  reachability (building on 2.4's per-backend health), and checkpointer
  init. Replace `logging.basicConfig(level=logging.INFO)` (`main.py:24`)
  with structured JSON logs (`structlog` or stdlib + JSON formatter)
  carrying a request/ticket correlation ID.
- **Why it matters:** Liveness vs. readiness is a standard k8s-adjacent
  distinction worth having correct even without k8s; structured logs with
  correlation IDs are the prerequisite for the observability layer in 3.4
  to be queryable at all.
- **Scope:** Small.
- **Files:** `app/api/main.py`, new `app/logging_config.py`.

### 3.4 Agent-aware observability (OpenTelemetry + Langfuse)
- **Build:** Instrument on OpenTelemetry using the GenAI semantic
  conventions so the backend stays swappable, exporting to Langfuse
  (self-hostable, MIT-licensed, good fit for a self-hosted no-vendor-lock
  project) or LangSmith. Track: latency per graph node
  (`plan`/`execute_step`/`finalize`), LLM token counts per call, MCP
  tool-call success/failure rate, and approval turnaround time
  (`Approval.created_at` → `decided_at` in `db/models.py`).
- **Why it matters:** This is the most interview-relevant deployment item
  after containerization — "I can show you a trace of a ticket from
  planning through approval to execution, with token cost and latency
  broken down per node" is a concrete, screenshot-able artifact, and
  GenAI-specific observability (vs. generic APM) is exactly the skill
  senior agent-engineering roles screen for.
- **Scope:** Medium.
- **Files:** `app/agent/graph.py` (span instrumentation), new
  `app/observability.py`, `pyproject.toml`.

### 3.5 Secrets manager instead of flat `.env`
- **Build:** Doppler (managed, near-zero setup) or Infisical (open-source,
  self-hostable) injecting env vars at container start; no code change
  needed since `pydantic-settings`' `Settings` class already reads from env.
- **Why it matters:** Small but real hygiene story; explicitly the
  lowest-effort item in this stage since it requires no application code
  changes at all.
- **Scope:** Small.
- **Files:** none in-app; `docker-compose.yml`/deployment config only.

**Stage 3 demo:** `docker compose up` brings up app + Postgres + MCP servers
from a clean machine, a CI badge shows green on the README, and a Langfuse
trace shows a full ticket lifecycle with per-node latency and token cost.

---

## Stage 4 — Real Identity & Enterprise Integration

**Theme:** replace every mock/shared-secret piece of identity and
authorization with something that mirrors how real IT-automation tools
integrate with HR/IdP/AD, and close the reliability gaps that only matter
once approvals and identity are real. This is the most enterprise-flavored
stage and the most legitimate to partially scope down for a solo project
(see trap notes below).

### 4.1 OIDC-backed reviewer identity (replaces free-text `reviewer` field)
- **Build:** Front the API with an OIDC provider (Keycloak self-hosted is
  the right portfolio choice — free, runs in a container, no vendor account
  needed vs. Auth0/Okta). `POST /approvals/{id}/decide` (`main.py:140-172`)
  extracts the reviewer from a verified JWT's `sub`/email claim via
  `fastapi-oidc` or equivalent, instead of trusting `payload.reviewer` as a
  free-text string (`main.py:153`).
- **Why it matters:** This is the sharpest, most quotable audit finding —
  "anyone holding the one shared API key can approve any sensitive action
  for any ticket" is a real security story with a real fix, and OIDC/JWT
  verification is squarely in senior-engineer territory.
- **Scope:** Medium.
- **Files:** `app/api/auth.py`, `app/api/main.py`, new `app/auth/oidc.py`,
  `docker-compose.yml` (Keycloak service).

### 4.2 Role/relationship-based approval authorization
- **Build:** Beyond "is authenticated," enforce "is this reviewer entitled
  to approve this specific action" — e.g., only the target employee's
  manager or a member of an `it-admin` group claim can decide a sensitive
  `disable_user` approval. Requires adding a minimal role/manager
  relationship to the identity model.
- **Why it matters:** Turns authentication into actual authorization —
  directly closes "no RBAC concept anywhere in the schema" from the audit.
  This is a natural, scoped extension of 4.1, not a separate system.
- **Scope:** Medium.
- **Files:** `app/db/models.py` (add role/manager fields), `app/api/main.py`,
  `app/api/auth.py`.

### 4.3 OAuth 2.1 resource-server auth per MCP server
- **Build:** For HTTP-transport MCP servers (per Stage 2), implement
  Protected Resource Metadata (RFC 9728) and audience-scoped bearer tokens
  (RFC 8707) per the MCP Authorization spec, replacing the single static
  `API_KEY`. Scope tokens per tool-domain (`identity:write`, `access:grant`)
  issued by the same Keycloak instance from 4.1. Note stdio-transport
  servers are explicitly spec-exempt from this — they pull credentials from
  the environment — so this only applies where Stage 2 chose HTTP
  transport.
- **Why it matters:** Turns `is_sensitive_action` from a DB row anyone with
  the shared key can flip into a real authorization decision
  (insufficient-scope → 403 with `WWW-Authenticate`), and demonstrates
  spec-literal MCP security knowledge, which is a differentiator most "I
  built an agent" portfolios don't have.
- **Scope:** Large.
- **Files:** each domain MCP server from Stage 2, `app/agent/mcp_client.py`,
  Keycloak client/scope config.

### 4.4 Identity as a read-through cache synced from an upstream system of record
- **Build:** Reframe `EmployeeUser` (`db/models.py:21-38`) as a cache table
  synced from a simulated upstream — the portfolio-appropriate version is a
  scheduled sync job pulling from a mock/sandbox Okta or a local OpenLDAP
  container via `ldap3`, not a real production HR/IdP integration. Writes
  to identity always go upstream, never directly into the cache.
- **Why it matters:** This is the architecturally correct pattern (HR → IdP
  → AD, cache never system-of-record) and demonstrates understanding of
  real enterprise identity flow, even though a solo project can't have a
  real Workday/Okta contract.
- **Scope:** Large (or Medium if scoped to "sync job + OpenLDAP container"
  rather than a real SCIM server).
- **Files:** new `app/identity/sync.py`, `app/db/models.py`,
  `docker-compose.yml` (OpenLDAP or mock-Okta container).

### 4.5 Approval SLA timeout + escalation
- **Build:** Attach an SLA timer to each `Approval` row; a scheduled sweep
  (APScheduler, since a full Temporal deployment is overkill here — see
  trap note) flags approvals past deadline and either auto-escalates to a
  secondary reviewer, auto-rejects, or notifies. This is also where the
  audit's "stuck ticket with no dead-letter/timeout" reliability gap gets
  closed — the same sweep can flag tickets stuck in `PLANNING`/`EXECUTING`
  past a threshold, surfaced via an admin endpoint.
- **Why it matters:** Reconciles two overlapping findings
  (approval-timeout from the identity research, stuck-ticket-detection from
  the reliability research) into one mechanism. "What happens if nobody
  ever clicks approve" is a natural human-in-the-loop interview question,
  and this is a concrete, scoped answer.
- **Scope:** Medium.
- **Files:** new `app/agent/sla_sweep.py`, `app/db/models.py` (add SLA
  deadline field to `Approval`), `app/api/main.py` (admin endpoint).

**Stage 4 demo:** log in as a specific reviewer via Keycloak, attempt to
approve an action outside your role scope and get denied, then show an
approval that times out and auto-escalates — end to end, from real identity
through to a policy-enforced decision.

---

## Highest-leverage next step to start with

**Start with Stage 1.3 (parallel `Send`-based fan-out) combined with 1.1
(retry/backoff), done together as one unit of work.**

Justification:
- **Cheapest to demo, biggest visible delta.** Both are pure in-process code
  changes against the *existing* SQLite + single-MCP-server setup — no new
  infrastructure, no new containers, no new external services. You can show
  a before/after latency number (sequential vs. parallel grants) and a
  fault-injection demo (kill the MCP process mid-call, watch it recover) in
  the same afternoon of work.
- **Most interview-relevant of everything in this roadmap.** "Walk me
  through how your agent handles a tool failure" and "how did you get
  concurrency out of a sequential planner" are near-universal senior-agent-
  engineer interview questions, and right now the honest answer to both is
  "it doesn't." These two features convert the weakest-sounding parts of
  the audit into concrete, narratable stories fastest.
- **Unlocks the most downstream work.** Session reuse (1.5) and the
  multi-server work in Stage 2 both compound on top of whatever call
  pattern you establish here — better to fix the retry/parallelism shape
  once, in the single-server world, before multiplying it across three MCP
  servers. Doing it in Stage 2 instead would mean debugging concurrency
  *and* multi-server routing at the same time.
- **Zero infra risk.** Everything else near the top of the list (Postgres,
  Docker, OIDC) either requires new services running locally or has a "did
  I configure this right" failure mode that's harder to isolate. Stage
  1.1/1.3 fail loudly and locally, in Python, which is the easiest place to
  debug them.

---

## Traps / overreach to explicitly avoid

- **Do not adopt Temporal.** The research surfaced Temporal wrapping
  LangGraph as "true durable execution" (nodes as Activities with
  auto-retry/timeout/heartbeat), and it's real, verifiable engineering —
  but standing up a Temporal cluster (or even Temporal Cloud) for a project
  whose entire failure surface is "an LLM call and 6 mock tool calls" is
  disproportionate. The retry/backoff (1.1) + idempotency (1.2) + SLA sweep
  (4.5) combination gets you 90% of the practical benefit — automatic
  recovery from transient failures and no silently-stuck tickets — without
  a new distributed system to operate, monitor, and explain in an interview
  as "why does my portfolio project need a workflow engine on top of its
  workflow engine." If asked about Temporal in an interview, the correct
  answer is "I evaluated it, and for this project's failure surface,
  LangGraph's `RetryPolicy` + a Postgres checkpointer covers the actual
  risk — Temporal would be the right call once you have long-running
  (multi-day) human-in-the-loop steps or truly distributed workers, which
  this doesn't have yet."

- **Do not adopt IBM mcp-context-forge (or any external MCP gateway
  product) as Stage 2's primary approach.** It's the correct answer once
  MCP servers need genuinely independent deploy/scale profiles (e.g., a
  real Jira API integration that needs its own container and rate-limit
  budget) — but for 3 servers that are still simulating backend systems
  with mock data, FastMCP's in-process `mount()` (2.1) plus a YAML registry
  (2.3) gets the same "multi-server, routed" story across in an interview
  without operating a second piece of gateway infrastructure. Mention the
  gateway pattern as "the next step if these were real integrations," not
  as something you built.

- **Do not build a full SCIM server or a real Workday/Okta integration.**
  Stage 4.4's "identity as read-through cache" is architecturally correct
  to describe, but a solo portfolio project has no real HR system to sync
  from. The right-sized version is a local OpenLDAP or mock-IdP container
  plus a scheduled sync job — enough to demonstrate the *pattern* (cache
  never system-of-record, writes go upstream) without pretending to have
  built enterprise IdP integration you can't actually validate against a
  real tenant.

- **Do not chase the official MCP Registry
  (`registry.modelcontextprotocol.io`) publication.** It's real and
  launched, but it's for servers meant to be discovered by *other*
  organizations' agents — irrelevant until this project's MCP servers are
  genuinely reusable outside this one repo. Worth knowing it exists (so you
  don't reinvent a worse version if asked), not worth building against.

- **Be honest about OAuth 2.1 scope (4.3) as the deepest, most spec-literal
  item in this roadmap** — it's high-value for the "I understand MCP's
  security model in real depth" story, but it's also the item most likely
  to eat disproportionate time on Keycloak scope/audience configuration
  edge cases relative to its demo payoff. If time-constrained, 4.1 (OIDC
  for human reviewers) alone already fixes the worst named finding (shared
  key approves anything); 4.3 (OAuth per MCP server) is the "go deeper"
  extension, not a blocking prerequisite.

- **Postgres migration (2.2) should happen exactly once, not twice.** Both
  the orchestration and deployment research threads proposed it
  independently (app DB and checkpointer respectively) — treat it as one
  migration covering both, done before Stage 3 containerization, not as two
  separate efforts. Containerizing while still on SQLite (skipping 2.2
  first) would just package the single-writer bug into a Docker image.
