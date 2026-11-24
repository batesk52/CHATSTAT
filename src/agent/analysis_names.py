"""Names of the analysis tools: they read saved files and never the instrument.

One list, used by the MCP server (these tools skip the driver lease) and by
``tools.dispatch_tool`` (these handlers run on a worker thread, so a few
seconds of plotting never stalls the asyncio loop that is waiting on a
measurement). Kept in its own module so neither user imports the other.
"""

from __future__ import annotations

__all__ = ["ANALYSIS_TOOLS"]

ANALYSIS_TOOLS = frozenset(
    {
        "load_session",
        "analyze_cv",
        "analyze_eis",
        "analyze_ca",
        "analyze_cp",
        "analyze_ecsa",
        "analyze_cic",
        "analyze_swv",
    }
)
