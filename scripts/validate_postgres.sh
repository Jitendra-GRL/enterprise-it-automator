#!/usr/bin/env bash
# End-to-end validation of the Postgres-backed deployment (Roadmap 2.2's
# missing proof): app DB on Postgres, LangGraph checkpointer on Postgres,
# HITL interrupt surviving a hard app restart, and the Alembic chain
# applying cleanly to a fresh Postgres database.
#
# Run anywhere Docker works (built for the case where the dev machine's
# Docker is unusable but a VM/CI runner is available):
#
#   GROQ_API_KEY=gsk_... ./scripts/validate_postgres.sh
#
# Required env: GROQ_API_KEY (or configure another LLM_PROVIDER's vars).
# POSTGRES_PASSWORD / API_KEY / MCP_SERVER_TOKEN are generated per run if
# unset. Exits nonzero on the first failed check; tears the stack down on
# exit either way (disable with KEEP_STACK=1).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GROQ_API_KEY:?Set GROQ_API_KEY (or edit this script for another provider)}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"
export API_KEY="${API_KEY:-$(openssl rand -hex 16)}"
export MCP_SERVER_TOKEN="${MCP_SERVER_TOKEN:-$(openssl rand -hex 16)}"
export GROQ_API_KEY

BASE_URL="http://127.0.0.1:8000"
PASS=0; FAIL=0
step() { printf '\n== %s\n' "$1"; }
ok()   { PASS=$((PASS+1)); printf '   OK: %s\n' "$1"; }
die()  { printf '   FAIL: %s\n' "$1"; exit 1; }

cleanup() {
  if [ "${KEEP_STACK:-0}" != "1" ]; then
    docker compose down -v >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

step "Build and start the Postgres-backed stack"
docker compose up -d --build
for i in $(seq 1 60); do
  if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && die "app never became healthy"
  sleep 3
done
ok "stack healthy"

step "Readiness probe verifies Postgres + checkpointer"
READY=$(curl -sf "$BASE_URL/ready") || die "/ready unreachable"
echo "$READY" | grep -q '"ready": *true' || die "/ready reports not-ready: $READY"
ok "/ready true (database + checkpointer)"

step "Checkpointer really is Postgres (checkpoints table exists)"
docker compose exec -T postgres psql -U itauto -d it_automator -tAc \
  "SELECT to_regclass('public.checkpoints') IS NOT NULL" | grep -q t \
  || die "no checkpoints table in Postgres — AsyncPostgresSaver not in use"
ok "LangGraph checkpoints table present in Postgres"

step "Seed demo employees + reviewers"
docker compose exec -T app python -m app.db.seed | tee /tmp/seed_out.txt
# Seed prints "  admin: <token_urlsafe>" — base64url alphabet, not hex.
ADMIN_TOKEN=$(grep -E '^  admin: ' /tmp/seed_out.txt | awk '{print $2}' | head -1 || true)
[ -n "$ADMIN_TOKEN" ] || ADMIN_TOKEN=$(docker compose exec -T postgres psql -U itauto -d it_automator -tAc \
  "SELECT token FROM reviewers WHERE role='IT_ADMIN' ORDER BY id LIMIT 1")
[ -n "$ADMIN_TOKEN" ] || die "could not obtain an it_admin reviewer token"
ok "seeded (reviewer token acquired)"

step "Submit an offboarding ticket (sensitive -> must gate, not execute)"
SUBMIT=$(curl -sf -X POST "$BASE_URL/tickets" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"requester":"hr@corp.example.com","subject":"Offboard jsmith",
       "body":"John Smith (jsmith) is leaving today. Disable his account."}')
echo "$SUBMIT" | grep -q '"interrupted": *true' || die "run did not pause at the HITL gate: $SUBMIT"
TICKET_ID=$(echo "$SUBMIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ticket_id"])')
ok "ticket $TICKET_ID gated at approval"

step "Hard-restart the app container (checkpoint durability across restart)"
docker compose restart app
for i in $(seq 1 30); do
  if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 30 ] && die "app did not come back after restart"
  sleep 3
done
ok "app restarted"

step "Approve after restart — graph must resume from the Postgres checkpoint"
APPROVAL_ID=$(curl -sf "$BASE_URL/approvals?status=pending" -H "X-API-Key: $API_KEY" \
  | python3 -c 'import json,sys; rows=json.load(sys.stdin); print(rows[0]["id"])')
DECIDE=$(curl -sf -X POST "$BASE_URL/approvals/$APPROVAL_ID/decide" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -H "X-Reviewer-Token: $ADMIN_TOKEN" -d '{"approve": true}')
echo "$DECIDE" | grep -q '"done": *true' || die "resume after restart did not complete: $DECIDE"
ok "approval resumed the checkpointed run to completion"

step "Ticket reached a terminal state with the action recorded"
curl -sf "$BASE_URL/tickets/$TICKET_ID" -H "X-API-Key: $API_KEY" \
  | grep -Eq '"status": *"(completed|failed)"' || die "ticket not terminal"
ok "ticket terminal"

step "Metrics endpoint live"
curl -sf "$BASE_URL/metrics" | grep -q "tickets_submitted_total" || die "/metrics missing counters"
ok "/metrics serving Prometheus exposition"

step "Alembic chain applies cleanly to a FRESH Postgres database"
docker compose exec -T postgres psql -U itauto -d it_automator -c \
  "DROP DATABASE IF EXISTS eit_alembic_check;" -c "CREATE DATABASE eit_alembic_check;" >/dev/null
docker compose exec -T \
  -e DATABASE_URL="postgresql+asyncpg://itauto:${POSTGRES_PASSWORD}@postgres:5432/eit_alembic_check" \
  app alembic upgrade head
docker compose exec -T postgres psql -U itauto -d eit_alembic_check -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | grep -Eq '[8-9]|1[0-9]' \
  || die "alembic upgrade produced too few tables"
ok "alembic upgrade head clean on fresh Postgres"

printf '\nAll %d checks passed — Postgres-backed deployment validated.\n' "$PASS"
