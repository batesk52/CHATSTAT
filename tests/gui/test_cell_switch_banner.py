"""The "waiting for cell switch" banner and the tool, with the real Qt bridge.

Headless (offscreen Qt), mock mode: the agent's coroutine runs on a worker
thread with its own event loop, the banner lives on the Qt thread, and the
test plays the person by clicking Continue or Cancel. No instrument and no
API key.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.cell_switch import (  # noqa: E402
    CellSwitchRequest,
    build_cell_switch_tool,
)
from src.agent.tools import ToolRegistry  # noqa: E402
from src.gui.agent_dock import AgentDockPanel  # noqa: E402
from src.gui.cell_switch_banner import CellSwitchBanner  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    bridge.install()
    yield app


def _request(medium="PBS, pH 7.4", reason="rinse after plating", rid="r1"):
    return CellSwitchRequest(rid, medium, reason, "2026-11-03T10:00:00+00:00")


# ---- the banner on its own ---------------------------------------------------------


def test_it_is_hidden_until_a_request_arrives(qapp) -> None:
    banner = CellSwitchBanner()
    assert banner.isHidden()
    assert banner.pending_request_id is None


def test_it_says_what_to_put_in_the_cell_and_why(qapp) -> None:
    banner = CellSwitchBanner()
    banner.present(_request(), lambda outcome: None)
    assert not banner.isHidden()
    assert banner._title.text() == "Waiting for cell switch"  # noqa: SLF001
    text = banner._message.text()  # noqa: SLF001
    assert "Switch the cell to PBS, pH 7.4" in text
    assert "rinse after plating" in text and "press Continue" in text
    assert banner.pending_request_id == "r1"


def test_continue_reports_a_confirmation_and_hides(qapp) -> None:
    banner = CellSwitchBanner(operator_fn=lambda: "someone")
    got = []
    banner.present(_request(), got.append)
    banner._continue_button.click()  # noqa: SLF001
    (outcome,) = got
    assert outcome.confirmed is True and outcome.operator == "someone"
    assert outcome.actuator == "human" and outcome.confirmed_at
    assert banner.isHidden() and banner.pending_request_id is None


def test_cancel_reports_a_refusal_and_hides(qapp) -> None:
    banner = CellSwitchBanner(operator_fn=lambda: "someone")
    got = []
    banner.present(_request(), got.append)
    banner._cancel_button.click()  # noqa: SLF001
    (outcome,) = got
    assert outcome.confirmed is False and outcome.operator == "someone"
    assert banner.isHidden()


def test_a_second_click_does_nothing(qapp) -> None:
    banner = CellSwitchBanner()
    got = []
    banner.present(_request(), got.append)
    banner._continue_button.click()  # noqa: SLF001
    banner._cancel_button.click()  # noqa: SLF001
    assert len(got) == 1


def test_withdraw_hides_without_answering(qapp) -> None:
    banner = CellSwitchBanner()
    got = []
    banner.present(_request(), got.append)
    banner.withdraw("other-id")                  # not this request: ignored
    assert not banner.isHidden() and not got
    banner.withdraw("r1")
    assert banner.isHidden() and banner.pending_request_id is None and not got


def test_a_new_request_declines_one_still_showing(qapp) -> None:
    banner = CellSwitchBanner()
    first, second = [], []
    banner.present(_request(rid="a"), first.append)
    banner.present(_request(medium="PBS again", rid="b"), second.append)
    assert [o.confirmed for o in first] == [False]
    assert second == [] and banner.pending_request_id == "b"
    assert "PBS again" in banner._message.text()  # noqa: SLF001


def test_a_failing_operator_name_does_not_block_the_answer(qapp) -> None:
    def broken():
        raise OSError("no user")

    banner = CellSwitchBanner(operator_fn=broken)
    got = []
    banner.present(_request(), got.append)
    banner._continue_button.click()  # noqa: SLF001
    assert got[0].confirmed is True and got[0].operator == "unknown"


# ---- in the dock and the window --------------------------------------------------------


def test_the_dock_has_the_banner_hidden(qapp) -> None:
    panel = AgentDockPanel(ToolRegistry())
    try:
        assert isinstance(panel.cell_switch_prompt, CellSwitchBanner)
        assert panel.cell_switch_prompt.isHidden()
    finally:
        panel.shutdown()


def test_the_window_registers_the_tool_and_its_prompt(qapp) -> None:
    window = MainWindow()
    try:
        assert "request_cell_switch" in window._agent_registry  # noqa: SLF001
        names = [d["name"] for d in window._agent_registry.tool_defs]  # noqa: SLF001
        assert names.count("request_cell_switch") == 1
    finally:
        window.close()


# ---- the tool with the real bridge: agent thread waits, Qt thread answers --------------


def _run_agent(make_coroutine, on_tick, limit_ms: int = 15000):
    """Run ``make_coroutine()`` on a worker thread while Qt spins here.

    ``on_tick`` runs on the Qt thread every 20 ms (the "person").
    """
    app = QCoreApplication.instance()
    results: list = []

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            results.append(loop.run_until_complete(make_coroutine()))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    ticker = QTimer()
    ticker.timeout.connect(on_tick)
    ticker.start(20)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.quit)
    guard.start(limit_ms)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(5.0)
    ticker.stop()
    guard.stop()
    assert not thread.is_alive(), "the agent coroutine did not finish"
    return results[0]


ARGS = {"new_medium": "PBS, pH 7.4", "reason": "rinse after plating"}


def _tool(banner, log=None):
    return build_cell_switch_tool(banner, log=log)


def test_the_agent_waits_on_the_banner_and_continues_when_it_is_pressed(qapp) -> None:
    banner = CellSwitchBanner(operator_fn=lambda: "lab_pc")
    events = []
    _, handler = _tool(banner, log=lambda kind, **f: events.append(kind))
    shown = []

    def person():
        if banner.pending_request_id and not shown:
            shown.append(banner._message.text())  # noqa: SLF001
            banner._continue_button.click()  # noqa: SLF001

    result = _run_agent(lambda: handler(dict(ARGS)), person)
    assert not isinstance(result, BaseException), result
    assert result["performed"] is True and result["confirmation"] == "confirmed"
    assert result["record"]["performed"]["operator"] == "lab_pc"
    assert "Switch the cell to PBS, pH 7.4" in shown[0]
    assert events == ["cell_switch_requested", "cell_switch_resolved"]
    assert banner.isHidden()


def test_the_agent_is_told_when_cancel_is_pressed(qapp) -> None:
    banner = CellSwitchBanner()
    _, handler = _tool(banner)

    def person():
        if banner.pending_request_id:
            banner._cancel_button.click()  # noqa: SLF001

    result = _run_agent(lambda: handler(dict(ARGS)), person)
    assert result["ok"] is False and result["performed"] is False
    assert result["confirmation"] == "cancelled"
    assert "NOT switched" in result["error"]
    assert banner.isHidden()


def test_stop_while_waiting_hides_the_banner_and_ends_the_wait(qapp) -> None:
    banner = CellSwitchBanner()
    events = []
    _, handler = _tool(banner, log=lambda kind, **f: events.append((kind, f)))

    async def scenario():
        task = asyncio.ensure_future(handler(dict(ARGS)))
        for _ in range(500):
            if banner.pending_request_id:
                break
            await asyncio.sleep(0.01)
        assert banner.pending_request_id, "the banner never showed the request"
        task.cancel()                               # the Stop button
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(500):                        # the withdraw runs on the Qt thread
            if banner.pending_request_id is None:
                break
            await asyncio.sleep(0.01)
        return banner.pending_request_id

    pending = _run_agent(scenario, lambda: None)
    assert pending is None
    assert banner.isHidden()
    assert [k for k, _ in events] == ["cell_switch_requested", "cell_switch_resolved"]
    assert events[1][1]["confirmation"] == "stopped"
