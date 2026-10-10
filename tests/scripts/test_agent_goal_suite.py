"""The goal-suite runner and its scorer, on the mock engine.

A fake client stands in for the model, so no key and no network are
needed; the mock engine really runs the measurement through the bridge,
the adapter and a worker QThread, exactly as a live suite would.
"""

from __future__ import annotations

import csv
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")
pytest.importorskip("yaml")

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import yaml  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from agent_goal_suite import run_suite  # noqa: E402
from goal_scoring import extract_resistances, score_answer  # noqa: E402
from src.agent.session_log import read_events  # noqa: E402

_SUITE_FILE = os.path.join(
    os.path.dirname(_SCRIPTS), "examples", "goal_suites", "resistor_test_cell.yaml"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _Stream:
    def __init__(self, final: SimpleNamespace) -> None:
        self._final = final

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            if False:
                yield None
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


def _tool_reply(n: int, name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(
            type="tool_use", id=f"toolu_{n}", name=name, input=tool_input
        )],
        stop_reason="tool_use",
    )


class _Client:
    """Replays a fixed list of replies; repeats the last one forever."""

    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self._replies = replies
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **_kwargs):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self) -> None:
        return None


def _rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_scorer_reads_channel_values_with_units() -> None:
    text = (
        "Channel 1: 1.00 kOhm\n"
        "| CH16 | 8.70 kOhm | ok |\n"
        "Channel 3 reads about 2 (no unit)\n"
    )
    assert extract_resistances(text) == {1: 1000.0, 16: 8700.0}
    scored = score_answer(
        text, {"resistance_ohm": {1: 1000, 16: 8660}, "tolerance_pct": 5}
    )
    assert scored["checks"] == 2 and scored["passed"] == 2
    assert score_answer(text, {}) is None
    assert score_answer("ch1 = 1.2 kOhm", {"resistance_ohm": {1: 1000}})[
        "passed"
    ] == 0


def test_example_suite_loads_and_has_plain_goals() -> None:
    with open(_SUITE_FILE, encoding="utf-8") as fh:
        suite = yaml.safe_load(fh)
    goals = suite["goals"]
    assert 4 <= len(goals) <= 6
    assert len({g["id"] for g in goals}) == len(goals)
    for goal in goals:
        assert goal["prompt"].strip()


def test_mock_suite_runs_logs_and_scores(qapp, tmp_path) -> None:
    suite = {
        "suite": "t",
        "goals": [{
            "id": "ch1",
            "prompt": "Measure the resistance on channel 1.",
            "repeats": 2,
            "expected": {"resistance_ohm": {1: 1000}},
        }],
    }

    def factory(**_kwargs):
        return _Client([
            _tool_reply(1, "device_status", {}),
            _tool_reply(2, "run_eis", {"channels": [1], "cr": "1m"}),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Channel 1: 1.01 kOhm")],
                stop_reason="end_turn",
            ),
        ])

    summary = run_suite(
        suite, str(tmp_path), mock=True, port=None, model="claude-fable-5",
        api_key="test-key", client_factory=factory,
    )
    rows = _rows(summary)
    assert [r["run"] for r in rows] == ["1", "2"]
    for row in rows:
        assert row["completed"] == "y"
        assert row["tool_calls"] == "2"
        assert row["score"] == "1.0"
        assert row["final_message"] == "Channel 1: 1.01 kOhm"
        events = read_events(row["log_path"])
        assert events[0]["goal_id"] == "ch1"
        assert events[0]["mode"] == "mock"
        assert events[-1]["event"] == "session_end"
        run = [e for e in events if e["event"] == "tool_result"][1]
        assert run["name"] == "run_eis" and run["result"]["ok"] is True
    assert rows[0]["log_path"] != rows[1]["log_path"]


def test_tool_call_limit_stops_the_session(qapp, tmp_path) -> None:
    suite = {"goals": [{"id": "loop", "prompt": "Keep checking.",
                        "max_tool_calls": 2}]}

    def factory(**_kwargs):
        # A model that never stops calling tools.
        return _Client([_tool_reply(1, "device_status", {})])

    rows = _rows(run_suite(
        suite, str(tmp_path), mock=True, port=None, model="claude-fable-5",
        api_key="test-key", client_factory=factory,
    ))
    assert rows[0]["completed"] == "n"
    assert rows[0]["stop_reason"] == "tool_call_limit"
    # The third call trips the limit before it is dispatched: it is
    # answered as cancelled, and no follow-up request is made.
    assert rows[0]["tool_calls"] == "2"
    events = read_events(rows[0]["log_path"])
    kinds = [e["event"] for e in events]
    assert kinds.count("tool_call") == 2
    assert "stop_requested" in kinds
    cancelled = [e for e in events if e.get("cancelled")]
    assert len(cancelled) == 1 and cancelled[0]["name"] == "device_status"
    assert kinds[-1] == "session_end"
    assert kinds.count("assistant_message") == 3
