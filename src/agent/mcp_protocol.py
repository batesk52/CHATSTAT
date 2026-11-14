"""The MCP side of the server: tools, the call handler and the prompt.

:func:`build_mcp_server` makes the SDK's low-level ``Server`` for one
registry. ``tools/list`` reads ``registry.tool_defs`` on every request (a tool
registered later is served too); ``tools/call`` goes to the
:class:`~src.agent.mcp_calls.ToolCallRunner`; the server instructions and the
prompt ``chatstat_rules`` carry :func:`~src.agent.mcp_guidance.build_instructions`.
"""

from __future__ import annotations

from typing import Any, Optional

import mcp.types as types
from mcp.server.lowlevel import Server

from src.agent.mcp_calls import ToolCallRunner
from src.agent.mcp_guidance import PROMPT_NAME, build_instructions
from src.agent.tools import ToolRegistry

__all__ = ["SERVER_NAME", "build_mcp_server"]

SERVER_NAME = "chatstat"


def build_mcp_server(
    registry: ToolRegistry, runner: ToolCallRunner, version: str = ""
) -> Server:
    """The low-level MCP server for *registry*, calls run by *runner*."""
    instructions = build_instructions(registry)
    server: Server = Server(
        SERVER_NAME, version=version or None, instructions=instructions
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=d["name"],
                description=d["description"],
                inputSchema=d["input_schema"],
            )
            for d in registry.tool_defs
        ]

    # validate_input=False: the handlers check their own input and give the
    # same refusal messages as in the app; a schema check here would answer
    # some calls differently.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await runner.call(server.request_context, name, arguments or {})

    @server.list_prompts()
    async def _list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(
                name=PROMPT_NAME,
                description=(
                    "How to use the ChatStat tools safely: the wiring check "
                    "and the operating rules the in-app agent follows."
                ),
            )
        ]

    @server.get_prompt()
    async def _get_prompt(
        name: str, arguments: Optional[dict[str, str]]
    ) -> types.GetPromptResult:
        if name != PROMPT_NAME:
            raise ValueError(f"Unknown prompt {name!r}; the prompt is {PROMPT_NAME!r}.")
        return types.GetPromptResult(
            description="ChatStat operating rules",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=instructions),
                )
            ],
        )

    return server
