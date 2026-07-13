"""Per-ticket LLM token budget — the cost-control half of running an agent
loop against a metered model. MAX_REPLANS already bounds how many times the
planner can run; this bounds the run in the currency that actually gets
billed (tokens), which matters because a few replans over a huge tool
reference/progress summary can cost more than many replans over a small one.

Mechanics: runner.start_ticket_run / resume_ticket_run install a
run-scoped accumulator (a ContextVar holding a mutable cell, so the graph's
nodes and the observability layer all see the same counter for THIS run and
concurrent ticket runs never share one). app/observability.py's
record_llm_call — the single choke point every LLM response already flows
through — feeds usage into it; plan/replan nodes persist the running total
into AgentState (so it checkpoints across HITL interrupts: the budget is
per-TICKET, not per-HTTP-request) and refuse to invoke the planner again
once the budget is spent, failing the ticket with an explicit error instead
of quietly continuing to spend.

Disabled by default (MAX_TOKENS_PER_TICKET=0) — a deliberate opt-in, since
the right ceiling is deployment-specific (model, prompt sizes, replan
budget) and a wrong-by-default ceiling would fail legitimate tickets.
"""

import logging
from contextvars import ContextVar

from app.config import get_settings

logger = logging.getLogger(__name__)

# A one-element list rather than a bare int: ContextVar values are
# immutable-per-set, but every party (nodes, record_llm_call) must see each
# other's increments within one run — a shared mutable cell does that
# without re-set() gymnastics at every call site.
_run_tokens: ContextVar[list[int] | None] = ContextVar("_run_tokens", default=None)


def start_accounting(initial_tokens: int = 0) -> None:
    """Installs this run's accumulator, seeded with the tokens the ticket
    has already spent (0 for a fresh run; the checkpointed state's
    tokens_used on resume — that's what makes the budget span interrupts).
    ContextVar scoping means each concurrent ticket run gets its own cell.
    """
    _run_tokens.set([initial_tokens])


def add_tokens(count: int) -> None:
    """Called by record_llm_call for every LLM response. Outside a ticket
    run (no accumulator installed — e.g. unit tests calling an LLM helper
    directly) this is a no-op rather than an error: token accounting is a
    ticket-run concern, not a precondition for calling an LLM.
    """
    cell = _run_tokens.get()
    if cell is not None and count > 0:
        cell[0] += count


def current_total() -> int | None:
    """This run's spend so far, or None when no accumulator is installed."""
    cell = _run_tokens.get()
    return cell[0] if cell is not None else None


def budget_exceeded() -> bool:
    """True when a budget is configured AND this run's accumulator says
    it's spent. With MAX_TOKENS_PER_TICKET=0 (default) or outside a run,
    always False — exactly the pre-budget behavior.
    """
    limit = get_settings().max_tokens_per_ticket
    if limit <= 0:
        return False
    total = current_total()
    return total is not None and total >= limit


def budget_error_message(ticket_id: int) -> str:
    limit = get_settings().max_tokens_per_ticket
    return (
        f"Ticket {ticket_id} exceeded its LLM token budget "
        f"({current_total()} used, limit {limit}) — aborting before further planner "
        "calls. Raise MAX_TOKENS_PER_TICKET or investigate why this ticket "
        "loops (see the replan history in the audit trail)."
    )
