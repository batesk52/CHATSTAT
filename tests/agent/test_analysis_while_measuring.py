"""The analysis tools read files only, so they answer during a measurement.

Seen on the instrument: load_session was refused ("Another MCP call is
driving the instrument") while a CV ran, so a client could not analyze one
export while the next measured. The analysis tools now skip the driver
lease; vendor_analysis.PYPLOT_LOCK keeps two callers from drawing at once.
"""

from __future__ import annotations

import threading

import src.agent.vendor_analysis as va
from src.agent.mcp_guidance import ANALYSIS_TOOLS, READ_ONLY_TOOLS
from src.agent.swv_analysis import build_swv_analysis_tools


def _analysis_names():
    names = {d["name"] for d, _ in va.build_analysis_tools(None)}
    names |= {d["name"] for d, _ in build_swv_analysis_tools(None)}
    return names


def test_every_analysis_tool_is_read_only_and_no_other_name_is_listed():
    assert ANALYSIS_TOOLS == _analysis_names()
    assert ANALYSIS_TOOLS <= READ_ONLY_TOOLS


def test_no_instrument_tool_became_read_only():
    for name in ("run_cv", "run_ca", "connect_device", "request_cell_switch",
                 "abort_measurement", "export_session"):
        assert name not in READ_ONLY_TOOLS


def test_a_handler_holds_the_pyplot_lock_while_it_runs():
    seen = {}

    def fn(args):
        seen["held"] = va.PYPLOT_LOCK.locked()
        return {"ok": True}

    va._safe("x", fn)({})   # noqa: SLF001
    assert seen["held"] is True
    assert va.PYPLOT_LOCK.locked() is False


def test_a_second_caller_waits_for_the_first():
    order = []
    started = threading.Event()
    release = threading.Event()

    def slow(args):
        order.append("a-start")
        started.set()
        release.wait(5)
        order.append("a-end")
        return {"ok": True}

    def quick(args):
        order.append("b")
        return {"ok": True}

    t = threading.Thread(target=va._safe("a", slow), args=({},))  # noqa: SLF001
    t.start()
    started.wait(5)
    u = threading.Thread(target=va._safe("b", quick), args=({},))  # noqa: SLF001
    u.start()
    u.join(0.2)
    assert order == ["a-start"]          # b is waiting on the lock
    release.set()
    t.join(5)
    u.join(5)
    assert order == ["a-start", "a-end", "b"]


def test_an_error_still_gives_the_lock_back():
    def boom(args):
        raise RuntimeError("no")

    out = va._safe("x", boom)({})   # noqa: SLF001
    assert out["ok"] is False
    assert va.PYPLOT_LOCK.locked() is False
