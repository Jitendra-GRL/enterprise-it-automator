"""Agent-side MCP client.

Talks to the custom MCP server over JSON-RPC, either by spawning it as a
local stdio subprocess (default, zero config) or by connecting to a remote
server over streamable-HTTP — the same two integration paths a real
orchestrator (e.g. watsonx Orchestrate) supports when registering an
external MCP tool server, as opposed to importing the tool functions
directly in-process. Selected by MCP_TRANSPORT (stdio|http) in config.
"""

import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.config import get_settings


@asynccontextmanager
async def mcp_session():
    settings = get_settings()
    if settings.mcp_transport == "http":
        async with streamable_http_client(settings.mcp_server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(session: ClientSession) -> list[dict]:
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in result.tools
    ]


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(name, arguments)
    if result.isError:
        text = "; ".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        raise RuntimeError(f"MCP tool {name!r} failed: {text}")
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    return None
