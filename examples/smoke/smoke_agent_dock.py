"""Smoke test: AgentDockPanel end to end, offscreen.

No network, no API key, no hardware.  Builds the mock engine +
connection + EngineAdapter + registry, then an AgentDockPanel whose
worker_factory returns an AgentWorker wired to a FAKE AsyncAnthropic
client (same scripted shape as examples/smoke/smoke_agent.py: text
deltas + a run_cv tool_use, then an end_turn reply once the tool_result
appears in the request history).

The scenario is driven through the REAL UI surface: the input field is
filled programmatically and the Send button is clicked.  When
agent_turn_done arrives (queued onto the GUI thread) the script
asserts:

* the transcript contains the user echo and both streamed texts;
* a tool card for run_cv exists and reached the 'done' state;
* input was re-enabled after the turn;
* a PNG pushed through the panel's figure_sink (the same callable the
  analysis tools receive) renders to a non-null pixmap;
* panel.shutdown() stops the worker thread cleanly.

A hard watchdog force-exits with code 2 after 60 s.  Prints
"SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_agent_dock.py
"""

from __future__ import annotations

# Eager-import native deps at module top, before any asyncio loop is
# created (avoids the Windows DLL-load deadlock).
import numpy  # noqa: F401  - eager native import
import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt  # noqa: E402

import io  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("ANTHROPIC_API_KEY", None)  # prove no key is needed

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import build_registry  # noqa: E402
from src.gui.agent_dock import AgentDockPanel  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_agent_dock")

WATCHDOG_SECONDS = 60.0
TOOL_USE_ID = "toolu_dock_01"
CV_INPUT = {
    "channels": [1],
    "e_begin": -0.2,
    "e_vertex1": 0.3,
    "e_vertex2": -0.2,
    "e_step": 0.05,
    "scan_rate": 0.5,
}
DELTAS_CALL1 = ["Starting ", "a CV on channel 1."]
DELTAS_CALL2 = ["The CV ", "finished cleanly."]
USER_TEXT = "please run a quick CV on channel 1"


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


def _make_png() -> bytes:
    """Render a tiny matplotlib Agg figure to PNG bytes."""
    fig, ax = plt.subplots(figsize=(2, 2))
    try:
        ax.plot([0, 1], [0, 1])
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=72)
        return buffer.getvalue()
    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------
# Fake AsyncAnthropic (adapted from examples/smoke/smoke_agent.py)
# ---------------------------------------------------------------------------

def _delta_event(text: str) -> SimpleNamespace:
    """One text_delta stream event."""
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


class _FakeStream:
    """Async context manager + async iterator over scripted events."""

    def __init__(self, deltas: list[str], final: SimpleNamespace) -> None:
        self._events = [_delta_event(t) for t in deltas]
        self._final = final

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


def _has_tool_result(messages: list) -> bool:
    """True when the request history already carries a tool_result."""
    for message in messages:
        content = (
            message.get("content") if isinstance(message, dict) else None
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
            ):
                return True
    return False


class FakeAsyncAnthropic:
    """Scripted two-call conversation: run_cv tool_use, then end_turn."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)
        self._final1 = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="".join(DELTAS_CALL1)),
                SimpleNamespace(
                    type="tool_use",
                    id=TOOL_USE_ID,
                    name="run_cv",
                    input=dict(CV_INPUT),
                ),
            ],
            stop_reason="tool_use",
        )
        self._final2 = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="".join(DELTAS_CALL2)),
            ],
            stop_reason="end_turn",
        )

    def _stream(self, **kwargs) -> _FakeStream:
        self.calls += 1
        if _has_tool_result(kwargs.get("messages", [])):
            return _FakeStream(DELTAS_CALL2, self._final2)
        return _FakeStream(DELTAS_CALL1, self._final1)


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []
    app = QApplication(sys.argv)
    bridge.install()

    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    adapter = EngineAdapter(engine, connection)
    registry = build_registry(adapter)

    fake = FakeAsyncAnthropic()

    def worker_factory(reg, api_key, model) -> AgentWorker:
        return AgentWorker(
            reg,
            api_key=api_key,
            model=model,
            client_factory=lambda **kwargs: fake,
        )

    panel = AgentDockPanel(registry, worker_factory=worker_factory)
    panel.resize(500, 700)
    panel.show()

    state = {"turns_done": 0}

    def on_turn_done() -> None:
        state["turns_done"] += 1
        app.quit()

    panel.worker.agent_turn_done.connect(on_turn_done)

    # Drive the REAL UI: set input text, click Send.
    panel._input_edit.setText(USER_TEXT)
    panel._send_button.click()

    if not panel.worker.isRunning():
        failures.append("worker thread did not start lazily on Send")

    app.exec()
    # Drain the queued tool_call_finished/figure events that may have
    # been emitted just before agent_turn_done.
    app.processEvents()

    if state["turns_done"] != 1:
        failures.append(f"turns_done != 1: {state['turns_done']}")

    transcript = panel.transcript_text()
    if f"You: {USER_TEXT}" not in transcript:
        failures.append(f"user echo missing from transcript: {transcript!r}")
    if "".join(DELTAS_CALL1) not in transcript or (
        "".join(DELTAS_CALL2) not in transcript
    ):
        failures.append(
            f"streamed text missing from transcript: {transcript!r}"
        )
    if "Agent:" not in transcript:
        failures.append(f"agent prefix missing: {transcript!r}")

    cards = panel.tool_card_states()
    run_cv_cards = [c for c in cards if c["name"] == "run_cv"]
    if len(run_cv_cards) != 1:
        failures.append(f"expected exactly one run_cv card: {cards!r}")
    elif run_cv_cards[0]["status"] != "done":
        failures.append(f"run_cv card not done: {run_cv_cards[0]!r}")

    if not panel._send_button.isEnabled():
        failures.append("send button still disabled after the turn")
    if not panel._input_edit.isEnabled():
        failures.append("input field still disabled after the turn")

    # The mock engine really ran the CV behind the tool card.
    if engine.result is None or engine.result.num_points <= 0:
        failures.append("mock engine did not produce CV data")

    # Push a PNG through the SAME sink callable the analysis tools get.
    png = _make_png()
    if not png.startswith(b"\x89PNG"):
        failures.append("test PNG generation failed")
    panel.figure_sink(
        {"title": "Smoke figure", "tool": "smoke", "png": png}
    )
    app.processEvents()  # queued signal -> GUI slot
    if panel.figure_count() != 1:
        failures.append(
            f"figure not rendered: count={panel.figure_count()}"
        )
    else:
        pixmap = panel.figure_label(0).pixmap()
        if pixmap is None or pixmap.isNull():
            failures.append("figure pixmap is null")

    panel.shutdown()
    if panel.worker.isRunning():
        failures.append("worker still running after shutdown()")
    panel.shutdown()  # idempotent

    if failures:
        for failure in failures:
            print("SMOKE FAIL:", failure)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()
    code = main()
    watchdog.cancel()
    sys.exit(code)
