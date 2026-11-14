"""An MCP client in a process of its own, for tests that kill it mid-call.

Usage: _mcp_client_process.py <url> <token> <tool> <json arguments>

Connects with the official client, calls the tool and waits for the answer.
A test kills this process while the call runs, so the server sees the
connection drop with no session DELETE and no cancel message. Not a test
module (the name does not start with test_).
"""

import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, token: str, tool: str, arguments: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(tool, json.loads(arguments))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))
