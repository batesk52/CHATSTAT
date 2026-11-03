"""Potential and duration limits on agent requests.

The pure check needs no Qt. The adapter and log tests use the mock engine
and a scripted model client: no instrument, no network, no API key.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.engine_adapter import EngineAdapter, build_technique_config  # noqa: E402
from src.agent.mock_engine import MockConnection, MockMeasurementEngine  # noqa: E402
from src.agent.request_limits import (  # noqa: E402
    DEFAULT_MAX_ABS_POTENTIAL_V,
    DEFAULT_MAX_DURATION_S,
    RequestLimits,
    check_request,
    estimate_duration_s,
    load_request_limits,
)
from src.agent.session_log import SessionLog, read_events  # noqa: E402
from src.agent.tools import build_registry, dispatch_tool  # noqa: E402
from src.data import app_settings  # noqa: E402
from src.techniques.scripts import generate, technique_params  # noqa: E402

LIMITS = RequestLimits()  # the defaults: +/-1 V, 14400 s (4 h)


def _check(technique: str, args: dict | None = None, channels=(1,)):
    """Check a request the way the adapter does: defaults plus the args."""
    config = build_technique_config(
        technique, {**(args or {}), "channels": list(channels)}
    )
    return check_request(config.technique, config.params, config.channels, LIMITS)


def _parameters(violations) -> list[str]:
    return [v.parameter for v in violations]


# ---- the pure check -------------------------------------------------------


@pytest.mark.parametrize("technique", ["cv", "ca", "swv", "eis"])
def test_the_default_request_of_every_agent_technique_passes(technique) -> None:
    assert _check(technique) == []


def test_a_request_inside_both_limits_passes() -> None:
    assert _check("ca", {"e_dc": 0.7, "t_run": 600.0}) == []
    assert _check("cv", {"e_begin": -0.9, "e_vertex2": -0.9, "e_vertex1": 0.9}) == []
    assert _check("eis", {"e_dc": 0.7, "freq_end": 1.0}) == []


def test_a_potential_over_the_limit_is_refused() -> None:
    found = _check("cv", {"e_vertex1": 1.5})
    assert _parameters(found) == ["e_vertex1"]
    only = found[0]
    assert only.kind == "potential"
    assert only.limit == DEFAULT_MAX_ABS_POTENTIAL_V
    assert only.requested == pytest.approx(1.5)
    assert "1.5" in only.message and "1" in only.message


def test_a_negative_potential_counts_by_its_size() -> None:
    assert _parameters(_check("ca", {"e_dc": -1.2})) == ["e_dc"]
    assert _check("ca", {"e_dc": -1.0}) == []  # exactly at the limit passes


def test_a_cv_from_minus_one_to_plus_one_volt_passes() -> None:
    # The limit is inclusive: |E| == 1.0 V is allowed, in both directions.
    args = {"e_begin": -1.0, "e_vertex1": 1.0, "e_vertex2": -1.0}
    assert _check("cv", args) == []
    assert _check("cv", {**args, "scan_rate": 0.05, "n_scans": 15}) == []
    # Anything visibly past it is refused, on either side.
    over = _check("cv", {**args, "e_vertex1": 1.000001})
    assert _parameters(over) == ["e_vertex1"]
    under = _check("cv", {"e_begin": -1.0001, "e_vertex1": 1.0, "e_vertex2": -1.0001})
    assert set(_parameters(under)) == {"e_begin", "e_vertex2"}


def test_a_square_wave_to_exactly_one_volt_is_refused_by_its_amplitude() -> None:
    # No amplitude on a CV, so +/-1.0 V passes there. A square wave from
    # -1.0 to +1.0 V reaches 1.025 V once the 25 mV amplitude rides on the
    # ends, which is beyond the +/-1 V range the scripts set.
    found = _check("swv", {"e_begin": -1.0, "e_end": 1.0})
    assert set(_parameters(found)) == {"e_begin", "e_end"}
    assert found[0].requested == pytest.approx(1.025)


def test_the_swing_on_top_of_a_potential_counts() -> None:
    # 0.99 V looks fine alone; with the 25 mV square wave it reaches 1.015 V.
    found = _check("swv", {"e_begin": -0.5, "e_end": 0.99, "amplitude": 0.025})
    assert _parameters(found) == ["e_end"]
    assert found[0].requested == pytest.approx(1.015)
    # 0.975 + 0.025 is exactly 1 V: float rounding must not refuse it.
    assert _check("swv", {"e_end": 0.975, "amplitude": 0.025}) == []
    assert _parameters(_check("eis", {"e_dc": 0.995, "e_ac": 0.01})) == ["e_dc"]


def test_a_step_larger_than_the_limit_is_refused() -> None:
    assert "e_step" in _parameters(_check("cv", {"e_step": 2.0}))


def test_every_over_limit_value_is_listed() -> None:
    found = _check("cv", {"e_vertex1": 2.0, "e_begin": -1.5, "e_vertex2": -1.5})
    assert set(_parameters(found)) >= {"e_vertex1", "e_begin", "e_vertex2"}


def test_a_duration_over_the_limit_is_refused() -> None:
    found = _check("ca", {"t_run": 20000.0})
    assert _parameters(found) == ["duration"]
    assert found[0].kind == "duration"
    assert found[0].limit == DEFAULT_MAX_DURATION_S == 14400.0
    assert found[0].requested == pytest.approx(20000.0)


def test_a_run_of_more_than_two_hours_goes_through() -> None:
    assert _check("ca", {"t_run": 3 * 3600.0}) == []  # 3 h, one channel
    assert _check("ca", {"t_run": 4 * 3600.0}) == []  # exactly the limit


def test_channels_and_equilibration_add_to_the_duration() -> None:
    # 1100 s on one channel is fine; on fourteen it is 15400 s.
    assert _check("ca", {"t_run": 1100.0}) == []
    assert _parameters(_check("ca", {"t_run": 1100.0}, channels=range(1, 15))) == [
        "duration"
    ]
    # Equilibration is spent once per channel.
    assert _parameters(_check("cv", {"t_eq": 8000.0}, channels=(1, 2))) == ["duration"]
    assert _check("cv", {"t_eq": 8000.0}, channels=(1,)) == []


def test_a_slow_scan_and_a_low_frequency_sweep_are_refused() -> None:
    assert _parameters(_check("cv", {"scan_rate": 0.0001})) == ["duration"]  # 20000 s
    assert _parameters(_check("cv", {"n_scans": 1000})) == ["duration"]  # 1000 x 20 s
    assert _parameters(_check("swv", {"frequency": 0.01})) == ["duration"]  # 20100 s
    assert _parameters(_check("eis", {"freq_end": 0.001})) == ["duration"]


def test_the_default_eis_sweep_against_the_four_hour_limit() -> None:
    # One default sweep (100 kHz to 0.1 Hz, 50 points) is about 15 min on
    # the bench (912 s) and 1053 s by the estimate. Thirteen channels one
    # after another fit under 4 h; fourteen or more do not, so the default
    # sweep on all sixteen channels is refused.
    assert _check("eis") == []
    assert _check("eis", channels=range(1, 14)) == []
    assert _parameters(_check("eis", channels=range(1, 15))) == ["duration"]
    found = _check("eis", channels=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
    assert found[0].requested == pytest.approx(14 * 1052.55, rel=1e-3)
    seconds, _ = estimate_duration_s("eis", technique_params("eis"), 16)
    assert seconds == pytest.approx(16840.8, rel=1e-3)  # 4.7 h; 4.06 h by the bench
    assert seconds > DEFAULT_MAX_DURATION_S


def test_the_duration_estimate_matches_the_bench() -> None:
    # From the goal-suite logs: CV -0.5 to 0.5 V and back at 0.1 V/s took
    # 24.7 s with about 4.6 s of start-up.
    seconds, _ = estimate_duration_s("cv", technique_params("cv"), 1)
    assert seconds == pytest.approx(20.0)
    # The default one-channel EIS sweep took 912.5 s (about 909 s of sweep).
    seconds, _ = estimate_duration_s("eis", technique_params("eis"), 1)
    assert 0.8 * 909 < seconds < 1.3 * 909


@pytest.mark.parametrize(
    "technique, name, bad",
    [
        ("ca", "e_dc", math.nan),
        ("ca", "e_dc", math.inf),
        ("ca", "e_dc", "0.5"),
        ("ca", "e_dc", True),
        ("ca", "t_run", math.nan),
        ("ca", "t_run", -5.0),
        ("cv", "scan_rate", 0.0),
        ("swv", "frequency", 0.0),
        ("eis", "freq_end", 0.0),
    ],
)
def test_a_value_that_cannot_be_checked_is_refused_not_passed(
    technique, name, bad
) -> None:
    found = _check(technique, {name: bad})
    assert found, f"{name}={bad!r} slipped through"
    assert {v.kind for v in found} == {"invalid"}


def test_tighter_limits_refuse_what_the_defaults_allow() -> None:
    config = build_technique_config("cv", {"channels": [1]})
    tight = RequestLimits(max_abs_potential_v=0.3, max_duration_s=5.0)
    found = check_request("cv", config.params, config.channels, tight)
    kinds = {v.kind for v in found}
    assert kinds == {"potential", "duration"}


def test_the_default_potential_limit_is_the_range_the_scripts_set() -> None:
    lines = generate("cv", technique_params("cv"), [1])
    (line,) = [ln for ln in lines if ln.startswith("set_pot_range")]
    low, high = (float(x) for x in re.findall(r"-?\d+\.?\d*", line))
    assert (low, high) == (-DEFAULT_MAX_ABS_POTENTIAL_V, DEFAULT_MAX_ABS_POTENTIAL_V)


# ---- where the limits live -------------------------------------------------


def test_limits_default_when_nothing_is_stored(tmp_path) -> None:
    got = load_request_limits(str(tmp_path / "none.json"))
    assert got == RequestLimits(DEFAULT_MAX_ABS_POTENTIAL_V, DEFAULT_MAX_DURATION_S)


def test_limits_come_from_the_settings_file(tmp_path) -> None:
    path = str(tmp_path / "s.json")
    app_settings.set_agent_max_abs_potential_v(0.8, path)
    app_settings.set_agent_max_duration_s(120, path)
    assert load_request_limits(path) == RequestLimits(0.8, 120.0)
    app_settings.set_agent_max_duration_s(None, path)  # clear -> default again
    assert load_request_limits(path).max_duration_s == DEFAULT_MAX_DURATION_S


@pytest.mark.parametrize("bad", [0, -1, "10", True, None, [1]])
def test_a_bad_stored_limit_falls_back_to_the_default(tmp_path, bad, caplog) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"agent_max_abs_potential_v": bad}), encoding="utf-8")
    got = load_request_limits(str(path))
    assert got.max_abs_potential_v == DEFAULT_MAX_ABS_POTENTIAL_V  # never off
    if bad is not None:
        assert "must be a positive number" in caplog.text


@pytest.mark.parametrize("bad", [0, -2.0, math.nan, math.inf])
def test_the_setters_refuse_a_limit_that_is_not_positive(tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        app_settings.set_agent_max_abs_potential_v(bad, str(tmp_path / "s.json"))
    with pytest.raises(ValueError):
        app_settings.set_agent_max_duration_s(bad, str(tmp_path / "s.json"))


def test_the_agent_has_no_tool_that_can_change_the_limits() -> None:
    adapter = EngineAdapter(SimpleNamespace(isRunning=lambda: False), None)
    registry = build_registry(adapter)
    for name in registry.names():
        assert "limit" not in name and "setting" not in name


# ---- the adapter ---------------------------------------------------------


class _NeverStartedEngine:
    """An engine that fails the test if a refused request reaches it."""

    def __init__(self) -> None:
        self.started = 0

    def isRunning(self) -> bool:  # noqa: N802
        return False

    def start_measurement(self, *_a, **_k) -> None:
        self.started += 1


def _run_async(coro_factory):
    return asyncio.run(coro_factory())


def test_an_over_limit_request_is_refused_and_never_started() -> None:
    engine = _NeverStartedEngine()
    adapter = EngineAdapter(engine, None, limits=lambda: LIMITS)
    result = _run_async(lambda: adapter.run_cv({"e_vertex1": 2.0}, channels=[1]))
    assert result["ok"] is False
    assert result["refused_by"] == "request_limits"
    assert result["sent_to_instrument"] is False
    assert result["limits"] == LIMITS.as_dict()
    (violation,) = result["violations"]
    assert violation["parameter"] == "e_vertex1"
    assert violation["limit"] == 1.0 and violation["requested"] == pytest.approx(2.0)
    # The model is told which value, the limit and what to do.
    assert "e_vertex1 = 2 V" in result["error"]
    assert "NOT sent" in result["error"]
    assert "cannot change these limits" in result["error"]
    assert engine.started == 0


def test_an_over_limit_duration_is_refused_and_never_started() -> None:
    engine = _NeverStartedEngine()
    adapter = EngineAdapter(engine, None, limits=lambda: LIMITS)
    result = _run_async(
        lambda: adapter.run_ca({"t_run": 20000.0}, channels=[1])
    )
    assert result["ok"] is False and result["refused_by"] == "request_limits"
    assert result["violations"][0]["kind"] == "duration"
    assert "20000" in result["error"] and "14400" in result["error"]
    assert engine.started == 0


def test_the_adapter_reads_the_limits_again_on_every_request() -> None:
    engine = _NeverStartedEngine()
    box = {"limits": RequestLimits(max_abs_potential_v=0.1)}
    adapter = EngineAdapter(engine, None, limits=lambda: box["limits"])
    first = _run_async(lambda: adapter.run_ca({"e_dc": 0.5}, channels=[1]))
    box["limits"] = RequestLimits(max_abs_potential_v=0.05)  # the person edits
    second = _run_async(lambda: adapter.run_ca({"e_dc": 0.5}, channels=[1]))
    assert first["limits"]["max_abs_potential_v"] == 0.1
    assert second["limits"]["max_abs_potential_v"] == 0.05
    assert engine.started == 0


_APP = None  # keeps the application alive for the whole module


def _qt_adapter():
    global _APP
    _APP = QCoreApplication.instance() or QApplication([])
    bridge.install()
    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    return engine, EngineAdapter(engine, connection, limits=lambda: LIMITS)


def _run_on_worker_thread(*factories):
    """Run coroutines on a worker thread while Qt spins here."""
    app = QCoreApplication.instance()
    results: list = []

    async def chain():
        for factory in factories:
            results.append(await factory())

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(chain())
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.quit)
    guard.start(30000)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(5.0)
    guard.stop()
    assert not thread.is_alive(), "the adapter call did not return"
    return results


def test_an_in_limit_request_runs_on_the_mock_engine() -> None:
    engine, adapter = _qt_adapter()
    refused, ran = _run_on_worker_thread(
        lambda: adapter.run_cv({"e_vertex1": 1.5}, channels=[1]),
        lambda: adapter.run_cv({"e_vertex1": 0.8}, channels=[1]),
    )
    assert refused["refused_by"] == "request_limits"
    assert ran["ok"] is True
    assert ran["num_points"] > 0


def test_a_cv_from_minus_one_to_plus_one_volt_runs_on_the_mock_engine() -> None:
    engine, adapter = _qt_adapter()
    (ran,) = _run_on_worker_thread(
        lambda: adapter.run_cv(
            {"e_begin": -1.0, "e_vertex1": 1.0, "e_vertex2": -1.0}, channels=[1]
        )
    )
    assert ran["ok"] is True, ran
    assert ran["num_points"] > 0


# ---- the session log -------------------------------------------------------


class _Stream:
    def __init__(self, final: SimpleNamespace) -> None:
        self._final = final

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            for block in self._final.content:
                if block.type == "text":
                    yield SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text=block.text),
                    )

        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


class _Client:
    """One tool call, then a final answer."""

    def __init__(self, tool_name: str, tool_input: dict) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        self._replies = [
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="Running it."),
                    SimpleNamespace(
                        type="tool_use", id="toolu_1", name=tool_name, input=tool_input
                    ),
                ],
                stop_reason="tool_use",
                usage=usage,
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Understood.")],
                stop_reason="end_turn",
                usage=usage,
            ),
        ]

    def _stream(self, **_kwargs):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self) -> None:
        return None


def _converse(tmp_path, tool_name: str, tool_input: dict, limits: RequestLimits):
    engine = _NeverStartedEngine()
    adapter = EngineAdapter(engine, None, limits=lambda: limits)
    registry = build_registry(adapter)
    log = SessionLog(str(tmp_path), app_version="0", mode="mock")
    client = _Client(tool_name, tool_input)
    worker = AgentWorker(
        registry,
        api_key="sk-ant-api03-" + "Zq7" * 30,
        model="claude-fable-5-1",
        session_log=log,
        client_factory=lambda **_k: client,
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        worker._loop, worker._queue = loop, queue
        main = asyncio.ensure_future(worker._main())
        queue.put_nowait("Run it.")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not worker.is_busy() and queue.empty() and log.started:
                break
        worker.request_stop()
        await asyncio.wait_for(main, timeout=5)

    asyncio.run(scenario())
    return read_events(log.path), engine


def test_a_refusal_is_logged(tmp_path) -> None:
    events, engine = _converse(
        tmp_path, "run_cv", {"channels": [1], "e_vertex1": 2.0}, LIMITS
    )
    assert engine.started == 0
    (refused,) = [e for e in events if e["event"] == "limit_refused"]
    assert refused["tool"] == "run_cv"
    assert refused["technique"] == "cv"
    assert refused["limits"] == LIMITS.as_dict()
    (violation,) = refused["violations"]
    assert violation["parameter"] == "e_vertex1"
    assert violation["requested"] == pytest.approx(2.0)
    assert violation["limit"] == 1.0
    # It follows the tool result it belongs to, and ids match.
    kinds = [e["event"] for e in events]
    call = next(e for e in events if e["event"] == "tool_call")
    result = next(e for e in events if e["event"] == "tool_result")
    assert refused["id"] == call["id"] == result["id"]
    assert kinds.index("limit_refused") == kinds.index("tool_result") + 1
    assert result["is_error"] is True
    assert result["result"]["refused_by"] == "request_limits"


def test_an_ordinary_tool_error_is_not_logged_as_a_refusal(tmp_path) -> None:
    events, _ = _converse(
        tmp_path, "run_cv", {"channels": [1], "bogus_parameter": 1}, LIMITS
    )
    assert [e for e in events if e["event"] == "limit_refused"] == []
    result = next(e for e in events if e["event"] == "tool_result")
    assert result["is_error"] is True


def test_dispatch_marks_a_refusal_as_an_error() -> None:
    adapter = EngineAdapter(_NeverStartedEngine(), None, limits=lambda: LIMITS)
    registry = build_registry(adapter)
    text, is_error = asyncio.run(
        dispatch_tool(registry, "run_ca", {"channels": [1], "e_dc": 3.0})
    )
    assert is_error is True
    assert json.loads(text)["refused_by"] == "request_limits"
