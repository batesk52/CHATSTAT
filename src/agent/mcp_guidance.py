"""What an outside MCP client is told: one source with the in-app prompt.

The in-app prompt is ``"You are the embedded lab assistant inside " +
INSTRUMENT_GUIDE`` plus the cell-switch and memory rules when those tools are
registered (``AgentWorker._effective_system_prompt``). The MCP server gives
the same text without the words that place the reader in the dock, plus
:data:`MCP_CLIENT_NOTE`, as its server instructions and as the MCP prompt
:data:`PROMPT_NAME`.
"""

from __future__ import annotations

from src.agent.agent_worker import INSTRUMENT_GUIDE
from src.agent.cell_switch import SYSTEM_PROMPT_RULE as CELL_SWITCH_RULE
from src.agent.cell_switch import TOOL_NAME as CELL_SWITCH_TOOL
from src.agent.memory_tools import LIST_TOOL_NAME, MEMORY_RULE_BODY
from src.agent.tools import ToolRegistry

__all__ = [
    "MCP_CLIENT_NOTE",
    "PROMPT_NAME",
    "READ_ONLY_TOOLS",
    "build_instructions",
]

#: The MCP prompt that carries the same guidance as the server instructions,
#: for clients that do not show server instructions to the model.
PROMPT_NAME = "chatstat_rules"

#: Tools that only read. They answer whoever else is driving, and they
#: neither need nor take the driver lease.
READ_ONLY_TOOLS = frozenset(
    {"device_status", "list_ports", "list_memories", "read_memory"}
)

#: What an outside client needs to know that the in-app agent does not.
MCP_CLIENT_NOTE = (
    "\nOver MCP. You reach these tools from outside the ChatStat window. A "
    "person may be at the window, and its own in-app agent may be working. "
    "One driver at a time: while the in-app agent is in a turn, a sequence "
    "runs, or another MCP call is running, every tool except device_status, "
    "list_ports, list_memories and read_memory is refused and nothing is "
    "run; wait and try again, or ask the person. While one of your calls "
    "runs, the in-app agent and the window's Start button are refused in "
    "the same way. A run while any measurement is running is refused "
    "('Engine is busy'). Do not call abort_measurement to clear that unless "
    "the person asked you to stop that run: it may be the person's own run. "
    "A call can take many minutes (an impedance sweep, or "
    "request_cell_switch, which waits for the person to press Continue or "
    "Cancel in the window); the server sends a progress notification every "
    "10 s while a call runs. If your connection drops during a call, the "
    "call is stopped: a measurement is aborted and the cell-switch prompt "
    "is taken down. Plots and analysis figures appear in the window, not "
    "in your results.\n"
)


def build_instructions(registry: ToolRegistry) -> str:
    """The guidance an MCP client gets, from the in-app prompt's own text."""
    text = "These tools run inside ChatStat, " + INSTRUMENT_GUIDE
    if CELL_SWITCH_TOOL in registry:
        text += CELL_SWITCH_RULE
    if LIST_TOOL_NAME in registry:
        text += MEMORY_RULE_BODY + ".\n"
    return text + MCP_CLIENT_NOTE
