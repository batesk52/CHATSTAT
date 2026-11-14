"""One driver at a time: who may drive the instrument right now.

The window has one instrument and up to three drivers: the person (Start,
the sequencer), the in-app agent, and an outside MCP client. The lease
records which agent-side driver holds the instrument, so a second one is
refused with a clear message instead of running at the same time.

GUI-thread only. Every check and take happens in ONE call on the GUI thread
(the callers reach it through ``bridge.run_on_gui``), so a check can never
be separated from its take by another driver.

Holders are strings: ``"dock"`` for the in-app agent's turn, and
``"mcp:<call id>"`` for one MCP call. The dock holds the lease from its
first tool that is not read-only to the end of its turn; an MCP call holds
it for the whole call.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "DOCK",
    "DriverLease",
    "MCP_DRIVING_MESSAGE",
    "is_mcp",
    "mcp_holder",
]

#: Holder name of the in-app agent's turn.
DOCK = "dock"

#: What the in-app agent, Start and the sequencer are told while an MCP
#: call holds the instrument.
MCP_DRIVING_MESSAGE = (
    "An MCP client is driving the instrument from outside the window. One "
    "driver at a time: nothing was run. Wait until its call has finished, "
    "or turn off File > Serve Agent Tools over MCP (that stops its call)."
)


def mcp_holder(call_id: str) -> str:
    """The holder name for one MCP call."""
    return f"mcp:{call_id}"


def is_mcp(holder: Optional[str]) -> bool:
    """True when *holder* is an MCP call."""
    return bool(holder) and str(holder).startswith("mcp:")


class DriverLease:
    """Which agent-side driver holds the instrument (GUI thread only)."""

    def __init__(self) -> None:
        self._holder: Optional[str] = None
        self._label: Optional[str] = None

    @property
    def holder(self) -> Optional[str]:
        """The current holder, or None when free."""
        return self._holder

    @property
    def label(self) -> Optional[str]:
        """A readable name of the holder (for example the MCP client)."""
        return self._label

    @property
    def held_by_mcp(self) -> bool:
        """True while an MCP call holds the instrument."""
        return is_mcp(self._holder)

    def try_take(self, holder: str, label: Optional[str] = None) -> bool:
        """Take the lease for *holder* if it is free or already theirs."""
        if self._holder is not None and self._holder != holder:
            return False
        self._holder = holder
        self._label = label or holder
        return True

    def release(self, holder: str) -> None:
        """Give the lease back; a release by anyone else is ignored."""
        if self._holder == holder:
            self._holder = None
            self._label = None
