"""Per-ticket-run MCP session cache.

Previously every tool call opened a fresh mcp_session() — on stdio transport,
a fresh subprocess spawn per call. For a plan with several steps that's
significant, avoidable overhead (subprocess startup dominates the actual
tool-call latency). This module lets one graph invocation (start_ticket_run
or resume_ticket_run) open a single session and have every node — including
concurrent fan-out branches — route tool calls through it instead.

THE ANYIO TASK-OWNERSHIP CONSTRAINT (why this isn't just "cache a session
object in a dict"): MCP's stdio transport (mcp.client.stdio.stdio_client)
opens an anyio.create_task_group() internally, and anyio cancel scopes MUST
be entered and exited in the exact same asyncio task — verified live: a
naive shared-session cache where get_or_open() ran in whatever task called
it first, and close() ran in runner.py's task, hit a real
"RuntimeError: Attempted to exit cancel scope in a different task than it
was entered in" the moment a LangGraph node (which LangGraph schedules on
its own task) touched the cached session. LangGraph nodes — including
concurrent Send()-based fan-out branches — do not all run in the same task
as runner.py's start_ticket_run/resume_ticket_run, so the session's owning
task and the callers' tasks are necessarily different.

THE FIX: a dedicated owner task holds the session for its entire open/close
lifecycle (both happen inside that one task, satisfying anyio's constraint)
and serves tool calls submitted by any other task through an asyncio.Queue —
a request/response proxy, not a shared object. Callers never touch the
ClientSession directly; they call SessionProxy.call_tool(...), which awaits
a per-request future that the owner task resolves.

Session reuse is scoped to a single graph.ainvoke() call, not across the
HITL interrupt/resume boundary: a live subprocess or network connection
cannot survive a process restart the way the DB-backed checkpoint state can,
so each resume_ticket_run() call after an approval opens its own fresh
owner task rather than trying to persist a live connection across pauses
that may last hours and cross process restarts.

TRADEOFF: the owner task opens its MCP session eagerly, as its first action,
before ticket_run_session() even yields — not lazily on first actual tool
call. An earlier lazy design (open on first get_or_open()) was simpler but
is exactly what caused the cross-task anyio bug above, since "whichever
task calls it first" is not deterministic. Opening eagerly, always inside
the owner task, sidesteps that entirely at the cost of a subprocess spawn
even for a ticket whose plan turns out empty (e.g. "no actions needed").
That's a worthwhile trade: correctness over a minor optimization for an
edge case, and it's still strictly better than the pre-Stage-1.5 baseline
of one subprocess PER TOOL CALL rather than per ticket.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.agent.mcp_client import call_tool as _call_tool, mcp_session

logger = logging.getLogger(__name__)


@dataclass
class _ToolCallRequest:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future


class SessionProxy:
    """Handle callers use to route tool calls through the owner task. Safe
    to call concurrently from multiple tasks — each call gets its own
    future; the owner task processes the queue one request at a time
    (MCP's ClientSession itself supports concurrent in-flight requests via
    request-ID correlation, but serializing through one queue keeps this
    proxy simple and avoids relying on that SDK internal).
    """

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    async def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._queue.put(_ToolCallRequest(tool=tool, args=args, future=future))
        return await future


_SHUTDOWN = object()


async def _owner_task_body(queue: asyncio.Queue, ready: asyncio.Event, failure: list):
    # Opens ONE session (no tool_name given, so registry.py resolves the
    # default/identity-domain location) and routes every tool call for this
    # ticket through it — correct today because every domain still resolves
    # to the same one gateway process. If registry.py ever maps different
    # domains to genuinely separate server processes, this owner task would
    # need to become one-session-per-domain-actually-used, not a single
    # shared session, since a session opened at the identity domain's
    # location couldn't serve a call meant for a different process.
    try:
        async with mcp_session() as session:
            ready.set()
            while True:
                item = await queue.get()
                if item is _SHUTDOWN:
                    break
                try:
                    result = await _call_tool(session, item.tool, item.args)
                    if not item.future.done():
                        item.future.set_result(result)
                except Exception as exc:
                    if not item.future.done():
                        item.future.set_exception(exc)
    except Exception as exc:
        failure.append(exc)
        if not ready.is_set():
            ready.set()


_active_proxies: dict[int, SessionProxy] = {}


@asynccontextmanager
async def ticket_run_session(ticket_id: int):
    """Spawns the owner task, waits for its MCP session to be ready (or for
    it to fail during setup), registers a SessionProxy other tasks can look
    up via get_cached_proxy(ticket_id), and on exit signals the owner task
    to close its session and shut down — all from this one function, so the
    open and close both happen inside the owner task itself.
    """
    queue: asyncio.Queue = asyncio.Queue()
    ready = asyncio.Event()
    failure: list[Exception] = []
    owner = asyncio.create_task(_owner_task_body(queue, ready, failure))

    await ready.wait()
    if failure:
        # Setup failed before the session was usable (e.g. subprocess
        # couldn't start) — surface it now rather than deferring to the
        # first call_tool(), which would otherwise hang forever.
        raise failure[0]

    _active_proxies[ticket_id] = SessionProxy(queue)
    try:
        yield _active_proxies[ticket_id]
    finally:
        _active_proxies.pop(ticket_id, None)
        await queue.put(_SHUTDOWN)
        await owner


def get_cached_proxy(ticket_id: int) -> SessionProxy | None:
    """Returns the SessionProxy for this ticket's active graph run, or None
    if none is active — callers must fall back to opening their own
    one-off mcp_session() in that case (e.g. a test invoking a node
    directly, or a future entry point that doesn't go through runner.py's
    session-caching wrapper)."""
    return _active_proxies.get(ticket_id)
