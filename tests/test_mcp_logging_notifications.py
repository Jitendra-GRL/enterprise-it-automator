"""Tests for server.py's _logged wrapper — verifies tool execution actually
sends MCP logging notifications (notifications/message) to a real connected
client, not just that the wrapper code exists. Spins up the real gateway
over streamable-HTTP (same pattern as test_mcp_transport.py's
test_streamable_http_round_trip) since logging notifications only have
somewhere to go once a real ClientSession with a logging_callback is
attached — calling mcp.list_tools()/read_resource() in-process (as the
other mcp_server tests do) never establishes a session for notifications to
flow through.

Resets server_module.mcp._session_manager before building the streamable-HTTP
app, same as test_mcp_gateway_auth.py's `_running_gateway` fixture and for
the same reason (see that file's comment): FastMCP lazily creates and caches
ONE StreamableHTTPSessionManager per instance, whose .run() can only execute
once — `mcp` is a module-level singleton every mcp_server test file shares,
so without this reset, running after another file that already started this
same app hits "SessionManager .run() can only be called once."
"""

import asyncio

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import LoggingMessageNotificationParams

from app.mcp_server.server import _bootstrap, mcp

_PORT = 8798


def _reset_db(monkeypatch):
    from app.config import get_settings
    from app.db import session as db_session_module

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None


async def test_tool_calls_emit_logging_notifications_to_connected_client(monkeypatch):
    """Covers both the success and failure paths of _logged in one real
    session — the module-level `mcp` FastMCP instance's streamable-HTTP app
    can only be started once per process (StreamableHTTPSessionManager
    enforces single-use), so a second, separate uvicorn server for a second
    test isn't an option here the way test_mcp_transport.py's round-trip
    test sidesteps it (that test builds its own standalone FastMCP
    instance, not this module's shared gateway).
    """
    _reset_db(monkeypatch)
    await _bootstrap()

    received: list[LoggingMessageNotificationParams] = []

    async def _on_log(params: LoggingMessageNotificationParams) -> None:
        received.append(params)

    mcp._session_manager = None
    # DNS-rebinding protection (mcp.settings.transport_security) is also
    # shared on this same `mcp` singleton — test_mcp_gateway_auth.py's own
    # tests reconfigure it with an allowlist scoped to whatever port THEY
    # used, which would otherwise reject this test's requests to _PORT with
    # a 421 "Invalid Host header" if that file ran first. Reconfigure it
    # here for this test's own port, same as
    # _authenticated_streamable_http_app() does for a real deployment.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{_PORT}"],
        allowed_origins=[],
    )
    config = uvicorn.Config(
        mcp.streamable_http_app(), host="127.0.0.1", port=_PORT, log_level="error"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)

        async with streamable_http_client(f"http://127.0.0.1:{_PORT}/mcp") as (read, write, _):
            async with ClientSession(read, write, logging_callback=_on_log) as session:
                await session.initialize()

                ok_result = await session.call_tool(
                    "identity_create_user",
                    {"username": "loguser", "full_name": "Log User", "email": "l@example.com"},
                )
                assert ok_result.isError is False

                err_result = await session.call_tool(
                    "identity_get_user", {"username": "nonexistent_user_for_log_test"}
                )
                assert err_result.isError is True

                # Give notifications a moment to arrive after each response.
                await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        await server_task

    create_messages = [p.data for p in received if p.logger == "identity_create_user"]
    assert any("invoked" in m for m in create_messages)
    assert any("completed" in m for m in create_messages)

    error_entries = [
        p for p in received if p.logger == "identity_get_user" and p.level == "error"
    ]
    assert error_entries
    assert "No such user" in error_entries[0].data
