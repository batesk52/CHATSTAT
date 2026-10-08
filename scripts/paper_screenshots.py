"""Render the paper screenshots (Figures 2, 3 and 5) offscreen.

Three figures of the running application, with NO hardware, NO API key and
NO network:

* ``main_window`` (Figure 2) -- the real ``MainWindow`` with the mock engine
  and mock connection swapped in for ``MeasurementEngine`` /
  ``PicoConnection``. It connects through the Connect button, selects all
  16 channels, starts one chronoamperometry (CA) run with the Start button
  and is captured after ``measurement_finished``.
* ``sequencer`` (Figure 3) -- a CV -> EIS -> CA sequence built in the
  Sequence tab from the three built-in presets and run through
  ``SequenceRunner`` on the mock engine, leaving one plot tab per step.
* ``agent_dock`` (Figure 5) -- the ``AgentDockPanel`` replaying the EIS
  auto-range sequence scripted in ``docs/agent_demo_eis_autorange.md``. The
  prompt is typed and sent through the real input box; the real
  ``AgentWorker`` loop streams a scripted model reply (fake client) and
  dispatches ``run_eis`` to a scripted handler that returns the demo doc's
  quality blocks. The last call is left running for the capture.

Every data point is synthetic (``src/agent/mock_engine.py``). Nothing under
``src/`` is modified; the swaps happen in this process only.

Rendering: Qt's offscreen platform at a 3x scale factor (``QT_SCALE_FACTOR``,
override it in the environment), so a 1200 px logical window saves as a
3600 px PNG. On Windows the offscreen platform has no font database (every
glyph renders as a box), so the script points it at the system fonts
(``QT_QPA_FONTDIR``) and sets Segoe UI 9 pt, the Windows default UI font.
The per-user preset store and app settings are redirected to a temporary
folder, so the real ``~/.emstat_pico_mux16`` is never read or written.

Run from the repo root:
    python scripts/paper_screenshots.py --figure all --out <folder>

PNGs land in ``<folder>/<figure>/attempt-NN/`` as
``echemcp_<figure>_attemptNN_<paper|slide>.png``.
"""

from __future__ import annotations

import os
import sys

# Environment knobs MUST be set before any PyQt6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "3")
if sys.platform == "win32":
    os.environ.setdefault(
        "QT_QPA_FONTDIR",
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    )
os.environ.pop("ANTHROPIC_API_KEY", None)  # prove no key is needed
os.environ.pop("EMSTAT_AGENT_MOCK", None)  # agent shares the window's engine

# Eager-import native deps at module top, before any asyncio loop is
# created (repo rule; avoids the Windows DLL-load deadlock).
import numpy  # noqa: E402,F401 - eager native import
import matplotlib  # noqa: E402

matplotlib.use("Agg", force=True)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import functools  # noqa: E402
import gc  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any, Callable  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from PyQt6.QtGui import QFont, QPen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

import src.gui.main_window as main_window_module  # noqa: E402
from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import ToolRegistry, build_tool_defs  # noqa: E402
from src.data import app_settings, presets  # noqa: E402
from src.data.sequence import SequenceStep  # noqa: E402
from src.gui.agent_dock import AgentDockPanel  # noqa: E402

FIGURES = ("main_window", "sequencer", "agent_dock")
MOCK_PORT = "MOCK1"

# Logical window sizes (px). 1200 px wide (the app's default width) keeps
# the 9 pt UI text at about 4.7 pt when a figure prints 6.5 in wide; the
# slide versions are 16:9. Figure 2 needs 1060 px of height: the Settings
# column is 950 px of panels plus the menu, dock tabs and status bar, and
# at the app's default 800 px it scrolls the channel grid and the
# measurement controls out of view. The agent dock is one portrait PNG
# that serves both the paper and slides.
MAIN_WINDOW_SIZES = {"paper": (1200, 1060), "slide": (1920, 1080)}
SEQUENCER_SIZES = {"paper": (1200, 800), "slide": (1280, 720)}
DOCK_SIZE = (600, 620)
LEFT_DOCK_WIDTH = 330
LOG_DOCK_HEIGHT = 130
# Print sizes written into the PNG's DPI metadata, so Inkscape and Word
# import each file at size: every paper/dock PNG shares one scale (1200
# logical px = 6.5 in, so the 600 px dock is a 3.25 in column), and the
# slide PNGs span a 13.333 in slide.
PAPER_IN_PER_PX = 6.5 / 1200
SLIDE_WIDTH_IN = 13.333

# Figure 2: one 16-channel CA. The mock ignores t_run and emits
# points_per_channel samples at t = k * t_interval, so 100 points at 10 s
# spans the 1000 s run the parameter panel shows. At 10 s the mock's
# 10 nA-per-channel offset is about 2 % of the plotted range, which keeps
# the 16 traces apart.
CA_PARAMS = {"t_interval": 10.0, "t_run": 1000.0}
CA_POINTS_PER_CHANNEL = 100

# Figure 3: the three built-in presets, channels 1-4 on every step. 31
# points per channel matches default_eis's n_freq (the EIS tab is shown).
SEQUENCE_PRESETS = ("default_cv", "default_eis", "default_ca")
SEQUENCE_CHANNELS = [1, 2, 3, 4]
SEQUENCE_POINTS_PER_CHANNEL = 31
SEQUENCE_SHOWN_STEP = 1  # 0-based: the EIS step and its plot tab

# Figure 5 text, verbatim from docs/agent_demo_eis_autorange.md: the
# combined opening prompt (markdown bold removed) and the two narration
# lines of the EIS scoping beat, with <N> = 6 (6u is the doc's example
# suggested_cr, one rung above 1u on the mode-3 ladder).
DEMO_PROMPT = (
    "I just connected a 16-channel device \u2014 gold electrodes with CeOx "
    "electrodeposited. Can you run basic electrochemical characterization on "
    "channels 1 through 4 \u2014 a CV and an EIS \u2014 then tell me which "
    "channels are working? What CV range is appropriate? Also, for EIS let's "
    "use a minimum frequency of 10 Hz so we can run them more quickly. I'm "
    "not sure which current range is appropriate for EIS \u2014 can you run a "
    "couple of scoping experiments on channel 1 first, starting at a low "
    "range and stepping it up until the spectrum is clean, then use that "
    "range to test all four channels?"
)
NARRATION_RERANGE = (
    "Channel 1 is under-ranged at 1 \u00b5A: the current exceeded the range. "
    "Stepping it up\u2026"
)
NARRATION_CLEAN = "Clean at 6 \u00b5A \u2014 using that for all four."

# (text streamed before the call, tool_use id, run_eis input) per model
# reply; 100 kHz -> 10 Hz as in the doc's expected tool-chip sequence.
DEMO_TURNS = [
    ("", "toolu_demo_01",
     {"channels": [1], "cr": "1u", "freq_start": 100000, "freq_end": 10}),
    (NARRATION_RERANGE, "toolu_demo_02",
     {"channels": [1], "cr": "6u", "freq_start": 100000, "freq_end": 10}),
    (NARRATION_CLEAN, "toolu_demo_03",
     {"channels": [1, 2, 3, 4], "cr": "6u", "freq_start": 100000,
      "freq_end": 10}),
]
# run_eis results, in call order. The first is the doc's example quality
# block; the doc gives no numbers for the clean re-run, so it carries only
# the verdict. The dock never displays results; they feed the replay loop.
EIS_RESULTS = [
    {
        "ok": True, "technique": "eis", "overload_points": 14,
        "quality_ok": False,
        "quality": {"1": {
            "points": 50, "overload_points": 14, "nan_points": 6,
            "neg_zreal_points": 8, "bad_fraction": 0.4,
            "verdict": "underranged",
        }},
        "suggested_cr": "6u", "rerange_exhausted": False,
        "quality_note": (
            "Channel(s) ['1'] look under-ranged at 1u ... Re-run those "
            "channel(s) at a LARGER current range, e.g. 6u, then continue."
        ),
    },
    {"ok": True, "technique": "eis", "quality_ok": True},
]


# ---------------------------------------------------------------------------
# Event-loop helpers
# ---------------------------------------------------------------------------

def _pump(
    app: QApplication, until: Callable[[], bool], timeout_s: float, what: str
) -> None:
    """Process Qt events until ``until()`` is true; raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while not until():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout_s:g} s: {what}")
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.001)


def _settle(app: QApplication, ms: int) -> None:
    """Keep the event loop running for ``ms`` (layouts, render timers)."""
    end = time.monotonic() + ms / 1000.0
    _pump(app, lambda: time.monotonic() >= end, ms / 1000.0 + 5.0, "settle")


def _save(widget: QWidget, path: str, width_in: float) -> tuple[int, int]:
    """Grab ``widget`` at the screen's pixel ratio; save a ``width_in`` PNG."""
    image = widget.grab().toImage()
    dots_per_meter = round(image.width() / (width_in * 0.0254))
    image.setDotsPerMeterX(dots_per_meter)
    image.setDotsPerMeterY(dots_per_meter)
    if image.isNull() or not image.save(path, "PNG"):
        raise RuntimeError(f"could not save {path}")
    return image.width(), image.height()


def _screen_line_weight(plot: Any) -> None:
    """Widen a live plot's curve pens by the screen's pixel ratio.

    pyqtgraph draws its 2 px curve pens in device pixels, so a 3x capture
    shows them at a third of their on-screen weight (0.26 pt hairlines at
    print size). Scaling restores the weight a 1x screen shows.
    """
    ratio = plot.devicePixelRatioF()
    for curve in plot._curves.values():
        pen = QPen(curve.opts["pen"])
        pen.setWidthF(pen.widthF() * ratio)
        curve.setPen(pen)


def _dispose(app: QApplication, widget: QWidget) -> None:
    """Close ``widget`` and delete it now, while the QApplication lives.

    MainWindow holds Python reference cycles (its file-dialog helper keeps
    the window), so left to the garbage collector it can be destroyed after
    the QApplication at interpreter exit -- an intermittent segfault.
    """
    widget.close()
    _settle(app, 100)
    sip.delete(widget)


def _isolate_user_data() -> str:
    """Point the preset store + app settings at a throwaway folder."""
    folder = tempfile.mkdtemp(prefix="paper_screenshots_")
    app_settings._DEFAULT_SETTINGS_FILE = os.path.join(
        folder, "app_settings.json"
    )
    presets._DEFAULT_PRESETS_FILE = os.path.join(folder, "presets.mux16")
    return folder


# ---------------------------------------------------------------------------
# Figures 2 and 3: MainWindow on the mock engine
# ---------------------------------------------------------------------------

def _build_main_window(
    points_per_channel: int, size: tuple[int, int], log_at_bottom: bool
) -> Any:
    """Construct the real MainWindow with the mock engine + connection."""
    main_window_module.MeasurementEngine = functools.partial(
        MockMeasurementEngine, points_per_channel=points_per_channel
    )
    main_window_module.PicoConnection = MockConnection
    window = main_window_module.MainWindow()
    # Headless: the modal end-of-run prompts are what a user clicks away.
    window._prompt_export = lambda result: None
    window._offer_sequence_save = lambda: None
    # The agent dock is Figure 5's subject; hiding it gives the plot room.
    window._agent_dock.hide()
    if log_at_bottom:
        # The Log dock opens tabbed behind Settings; move it under the plot
        # (a user can drag it there) and keep the left column full height.
        window.setCorner(
            Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea
        )
        window.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea, window._log_dock
        )
        window._log_dock.show()
    window.resize(*size)
    window.show()
    window.resizeDocks(
        [window._control_dock], [LEFT_DOCK_WIDTH], Qt.Orientation.Horizontal
    )
    if log_at_bottom:
        window.resizeDocks(
            [window._log_dock], [LOG_DOCK_HEIGHT], Qt.Orientation.Vertical
        )
    return window


def _connect_mock(app: QApplication, window: Any) -> None:
    """Connect through the Connection panel's own Connect button."""
    combo = window._conn_panel._port_combo
    combo.clear()
    combo.addItem(MOCK_PORT, MOCK_PORT)
    window._conn_panel._connect_btn.click()
    _pump(
        app,
        lambda: window._connection.is_connected
        and window._connect_worker is None,
        10.0,
        "mock connect",
    )


def render_main_window(
    app: QApplication, size: tuple[int, int], path: str, width_in: float
) -> tuple[int, int]:
    """Figure 2: 16-channel mock CA in the main window."""
    window = _build_main_window(CA_POINTS_PER_CHANNEL, size, True)
    try:
        _settle(app, 200)
        scroll = window._control_dock.widget()
        needed = scroll.widget().sizeHint().height()
        if scroll.viewport().height() < needed:
            raise RuntimeError(
                f"Settings column clipped: {scroll.viewport().height()} px "
                f"shown, {needed} px needed; raise the window height"
            )
        _connect_mock(app, window)
        window._tech_panel.set_technique("ca")
        window._tech_panel.set_params(CA_PARAMS)
        window._chan_panel.select_all()
        finished: list[object] = []
        window._engine.measurement_finished.connect(finished.append)
        window._meas_panel._start_btn.click()
        _pump(app, lambda: bool(finished), 180.0, "CA measurement_finished")
        _settle(app, 400)
        plot = window._plot_container.nyquist
        counts = {ch: len(xs) for ch, xs in plot._x_data.items()}
        if sorted(counts) != list(range(1, 17)) or set(counts.values()) != {
            CA_POINTS_PER_CHANNEL
        }:
            raise RuntimeError(f"live plot is not 16 full traces: {counts}")
        _screen_line_weight(plot)
        # The default top-left legend covers the steep start of the 16
        # traces; move it to the empty top-right, where a user can drag it
        # (pyqtgraph legends are draggable).
        plot.getPlotItem().legend.setOffset((-10, 10))
        _settle(app, 100)
        return _save(window, path, width_in)
    finally:
        _dispose(app, window)


def render_sequencer(
    app: QApplication, size: tuple[int, int], path: str, width_in: float
) -> tuple[int, int]:
    """Figure 3: CV -> EIS -> CA sequence run on the mock engine."""
    window = _build_main_window(SEQUENCE_POINTS_PER_CHANNEL, size, False)
    try:
        window._sequence_dock.raise_()
        _settle(app, 200)
        _connect_mock(app, window)
        panel = window._sequence_panel
        techniques = []
        for key in SEQUENCE_PRESETS:
            step = SequenceStep.from_preset(
                key, window._preset_mgr.get_preset(key)
            )
            step.channels = list(SEQUENCE_CHANNELS)
            panel.add_step(step)
            techniques.append(step.technique.upper())
        # Expand the EIS block so its per-step editor shows.
        panel._step_widgets()[SEQUENCE_SHOWN_STEP]._toggle.setChecked(True)
        stopped: list[bool] = []
        panel.sequence_stopped.connect(lambda: stopped.append(True))
        panel._run_btn.click()
        _pump(app, lambda: bool(stopped), 180.0, "sequence_stopped")
        _settle(app, 400)
        tabs = window._plot_tabs
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        expected = [f"{i}\u00b7{t}" for i, t in enumerate(techniques, 1)]
        if labels != expected:
            raise RuntimeError(f"plot tabs {labels} != {expected}")
        for i in range(tabs.count()):
            _screen_line_weight(tabs.widget(i).nyquist)
        tabs.setCurrentIndex(SEQUENCE_SHOWN_STEP)
        _settle(app, 300)
        return _save(window, path, width_in)
    finally:
        _dispose(app, window)


# ---------------------------------------------------------------------------
# Figure 5: agent dock replaying the scripted demo
# ---------------------------------------------------------------------------

def _tool_result_count(messages: list) -> int:
    """Number of tool_result blocks already in the request history."""
    count = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            count += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
    return count


class _ScriptedStream:
    """Async context manager + iterator over one scripted model reply."""

    def __init__(self, text: str, final: SimpleNamespace) -> None:
        # Word-sized deltas, as a streamed reply arrives.
        self._chunks = [w + " " for w in text.split(" ")] if text else []
        if self._chunks:
            self._chunks[-1] = self._chunks[-1].rstrip(" ")
        self._final = final

    async def __aenter__(self) -> "_ScriptedStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=chunk),
                )
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


class _ScriptedDemoClient:
    """Stands in for AsyncAnthropic: replays DEMO_TURNS in order."""

    def __init__(self) -> None:
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs: Any) -> _ScriptedStream:
        done = _tool_result_count(kwargs.get("messages", []))
        if done >= len(DEMO_TURNS):
            # Only reached after shutdown cancels the last call.
            return _ScriptedStream(
                "", SimpleNamespace(content=[], stop_reason="end_turn")
            )
        text, tool_id, tool_input = DEMO_TURNS[done]
        content = [SimpleNamespace(type="text", text=text)] if text else []
        content.append(SimpleNamespace(
            type="tool_use", id=tool_id, name="run_eis", input=dict(tool_input)
        ))
        return _ScriptedStream(
            text, SimpleNamespace(content=content, stop_reason="tool_use")
        )


class _ScriptedRunEis:
    """run_eis handler returning EIS_RESULTS in order.

    The call after the last result never returns on its own: its chip stays
    "running" for the capture and ``panel.shutdown()`` cancels it.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, tool_input: dict) -> dict:
        self.calls += 1
        if self.calls <= len(EIS_RESULTS):
            return dict(EIS_RESULTS[self.calls - 1])
        await asyncio.sleep(3600.0)
        return {"ok": False, "error": "replay ended"}


def render_agent_dock(
    app: QApplication, size: tuple[int, int], path: str, width_in: float
) -> tuple[int, int]:
    """Figure 5: the EIS auto-range beat in the agent dock."""
    registry = ToolRegistry()
    for tool_def in build_tool_defs():
        if tool_def["name"] == "run_eis":  # the real schema, scripted handler
            registry.register(tool_def, _ScriptedRunEis())
    client = _ScriptedDemoClient()

    def worker_factory(reg, api_key, model) -> AgentWorker:
        return AgentWorker(
            reg, api_key=api_key, model=model,
            client_factory=lambda **kwargs: client,
        )

    panel = AgentDockPanel(registry, worker_factory=worker_factory)
    try:
        panel.resize(*size)
        panel.show()
        _settle(app, 200)
        panel._input_edit.setText(DEMO_PROMPT)
        panel._send_button.click()

        def _last_call_running() -> bool:
            states = [c["status"] for c in panel.tool_card_states()]
            return states == ["done", "done", "running"] and (
                NARRATION_CLEAN in panel.transcript_text()
            )

        _pump(app, _last_call_running, 30.0, "the replay's last run_eis")
        _settle(app, 300)  # let the chat lay out the last chip
        # The chat follows the newest message, so a transcript taller than
        # the viewport would scroll the prompt out of the top of the frame.
        if panel._chat.verticalScrollBar().maximum() > 0:
            raise RuntimeError("transcript taller than the chat; raise height")
        # Same blink frame every run: "running.." with the accent border.
        _pump(app, lambda: panel._chat._blink_phase % 4 == 2, 5.0, "blink")
        return _save(panel, path, width_in)
    finally:
        panel.shutdown()
        worker = panel.worker
        _dispose(app, panel)
        if not worker.isRunning():
            sip.delete(worker)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

RENDERERS = {
    "main_window": (render_main_window, MAIN_WINDOW_SIZES),
    "sequencer": (render_sequencer, SEQUENCER_SIZES),
    "agent_dock": (render_agent_dock, {"paper": DOCK_SIZE}),
}


def main(argv: list[str] | None = None) -> int:
    """Render the requested figures. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--figure", choices=(*FIGURES, "all"), default="all",
        help="Which figure to render (default: all).",
    )
    parser.add_argument(
        "--out", required=True,
        help="Root folder; PNGs land in <out>/<figure>/attempt-NN/.",
    )
    parser.add_argument(
        "--attempt", type=int, default=1, help="Attempt number (default 1)."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv[:1])
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 9))
    user_dir = _isolate_user_data()
    figures = FIGURES if args.figure == "all" else (args.figure,)
    try:
        for figure in figures:
            render, sizes = RENDERERS[figure]
            folder = os.path.join(
                args.out, figure, f"attempt-{args.attempt:02d}"
            )
            os.makedirs(folder, exist_ok=True)
            for version, size in sizes.items():
                name = f"echemcp_{figure}_attempt{args.attempt:02d}_{version}.png"
                path = os.path.join(folder, name)
                width_in = (
                    SLIDE_WIDTH_IN if version == "slide"
                    else size[0] * PAPER_IN_PER_PX
                )
                width, height = render(app, size, path, width_in)
                print(
                    f"saved {path}  {width} x {height} px "
                    f"(logical {size[0]} x {size[1]}, {width_in:.2f} in wide)",
                    flush=True,
                )
    finally:
        gc.collect()  # any leftover Qt objects go while the app exists
        shutil.rmtree(user_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
