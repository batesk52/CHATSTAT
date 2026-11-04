"""request_cell_switch: the agent asks for a solution change and waits.

No Qt event loop, no instrument and no API key: the GUI thread is replaced by
a direct call, the actuator by a fake prompt, and the model by a scripted
client. The Qt side (the banner and the bridge) is tested in
``tests/gui/test_cell_switch_banner.py``.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent import cell_switch as cs  # noqa: E402
from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.cell_switch import (  # noqa: E402
    SYSTEM_PROMPT_RULE,
    TOOL_NAME,
    CellSwitchOutcome,
    build_cell_switch_tool,
)
from src.agent.session_log import SessionLog, read_events  # noqa: E402
from src.agent.tools import ToolRegistry, dispatch_tool  # noqa: E402


def _direct(fn, *args, **kwargs):
    """Stands in for ``run_on_gui``: call at once, return an awaitable."""
    future = asyncio.get_running_loop().create_future()
    try:
        future.set_result(fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        future.set_exception(exc)
    return future


class FakePrompt:
    """An actuator that answers when the test says so."""

    def __init__(self) -> None:
        self.presented = []
        self.withdrawn = []
        self._resolve = None

    def present(self, request, resolve) -> None:
        self.presented.append(request)
        self._resolve = resolve

    def withdraw(self, request_id) -> None:
        self.withdrawn.append(request_id)

    def answer(self, confirmed=True, operator="op", stamp="2026-11-03T12:00:00+00:00",
               actuator="human") -> None:
        self._resolve(CellSwitchOutcome(confirmed, operator, stamp, actuator))


class Log:
    def __init__(self) -> None:
        self.events = []

    def __call__(self, kind, **fields) -> None:
        self.events.append((kind, fields))

    def kinds(self):
        return [k for k, _ in self.events]


def _tool(prompt=None, is_busy=None, log=None):
    prompt = prompt or FakePrompt()
    log = log if log is not None else Log()
    tool_def, handler = build_cell_switch_tool(
        prompt, is_busy=is_busy, log=log, call_on_gui=_direct
    )
    return tool_def, handler, prompt, log


async def _until(predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


ARGS = {"new_medium": "Pt-black plating bath", "reason": "next step is the deposition"}


# ---- the tool definition ------------------------------------------------------


def test_the_tool_definition_asks_for_a_medium_and_a_reason() -> None:
    tool_def, *_ = _tool()
    assert tool_def["name"] == TOOL_NAME == "request_cell_switch"
    schema = tool_def["input_schema"]
    assert set(schema["properties"]) == {"new_medium", "reason"}
    assert schema["required"] == ["new_medium", "reason"]
    assert schema["additionalProperties"] is False
    text = tool_def["description"]
    assert "BEFORE" in text and "WAIT" in text and "performed" in text


def test_it_registers_beside_the_other_tools() -> None:
    tool_def, handler, *_ = _tool()
    registry = ToolRegistry()
    registry.register(tool_def, handler)
    assert TOOL_NAME in registry
    assert registry.tool_defs[0]["name"] == TOOL_NAME


# ---- confirmed ----------------------------------------------------------------


def test_the_agent_waits_until_the_person_confirms() -> None:
    async def scenario():
        _, handler, prompt, log = _tool()
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        assert not task.done()                      # still waiting
        (request,) = prompt.presented
        assert request.new_medium == "Pt-black plating bath"
        assert request.reason == "next step is the deposition"
        # The request is on the log BEFORE the wait ends.
        assert log.kinds() == ["cell_switch_requested"]
        prompt.answer(True, operator="lab_pc")
        result = await asyncio.wait_for(task, 2)
        return result, log, request

    result, log, request = asyncio.run(scenario())
    assert result["ok"] is True and result["performed"] is True
    assert result["confirmation"] == "confirmed"
    assert "You may continue" in result["message"]
    assert log.kinds() == ["cell_switch_requested", "cell_switch_resolved"]
    record = result["record"]
    assert record["record"] == "cell_switch"
    assert record["request_id"] == request.request_id
    assert record["requested"] == {
        "medium": "Pt-black plating bath",
        "reason": "next step is the deposition",
        "timestamp": request.requested_at,
    }
    assert record["performed"] == {
        "done": True,
        "timestamp": "2026-11-03T12:00:00+00:00",
        "operator": "lab_pc",
        "actuator": "human",
    }
    assert record["confirmation"] == "confirmed"
    assert record["waited_s"] >= 0
    assert log.events[1][1] == record               # the log holds the same record


def test_a_robot_can_answer_through_the_same_record() -> None:
    async def scenario():
        _, handler, prompt, _ = _tool()
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        prompt.answer(True, operator="arm-1", actuator="robot")
        return await asyncio.wait_for(task, 2)

    performed = asyncio.run(scenario())["record"]["performed"]
    assert performed["actuator"] == "robot" and performed["operator"] == "arm-1"


def test_only_the_first_answer_counts() -> None:
    async def scenario():
        _, handler, prompt, log = _tool()
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        prompt.answer(True)
        prompt.answer(False)                         # a late second click
        return await asyncio.wait_for(task, 2), log

    result, log = asyncio.run(scenario())
    assert result["performed"] is True
    assert log.kinds().count("cell_switch_resolved") == 1


# ---- cancelled by the person ---------------------------------------------------


def test_cancel_tells_the_agent_the_cell_was_not_switched() -> None:
    async def scenario():
        _, handler, prompt, log = _tool()
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        prompt.answer(False, operator="lab_pc")
        return await asyncio.wait_for(task, 2), log

    result, log = asyncio.run(scenario())
    assert result["ok"] is False and result["performed"] is False
    assert result["confirmation"] == "cancelled"
    assert "NOT switched" in result["error"] and "Pt-black plating bath" in result["error"]
    assert "ask how to proceed" in result["error"]
    record = result["record"]
    assert record["performed"]["done"] is False
    assert record["confirmation"] == "cancelled"
    assert log.kinds() == ["cell_switch_requested", "cell_switch_resolved"]


def test_a_cancel_is_flagged_as_an_error_by_the_dispatcher() -> None:
    async def scenario():
        tool_def, handler, prompt, _ = _tool()
        registry = ToolRegistry()
        registry.register(tool_def, handler)
        task = asyncio.ensure_future(dispatch_tool(registry, TOOL_NAME, dict(ARGS)))
        await _until(lambda: prompt.presented)
        prompt.answer(False)
        return await asyncio.wait_for(task, 2)

    text, is_error = asyncio.run(scenario())
    assert is_error is True and '"performed":false' in text


# ---- Stop pressed while waiting --------------------------------------------------


def test_stop_while_waiting_takes_the_prompt_down_and_logs_it() -> None:
    async def scenario():
        _, handler, prompt, log = _tool()
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return prompt, log

    prompt, log = asyncio.run(scenario())
    (request,) = prompt.presented
    assert prompt.withdrawn == [request.request_id]
    assert log.kinds() == ["cell_switch_requested", "cell_switch_resolved"]
    resolved = log.events[1][1]
    assert resolved["confirmation"] == "stopped"
    assert resolved["performed"]["done"] is False
    assert resolved["performed"]["operator"] is None


# ---- refusals ----------------------------------------------------------------------


def test_it_refuses_while_a_measurement_is_running() -> None:
    async def scenario():
        _, handler, prompt, log = _tool(is_busy=lambda: True)
        return await handler(dict(ARGS)), prompt, log

    result, prompt, log = asyncio.run(scenario())
    assert result["ok"] is False and "measurement is running" in result["error"]
    assert prompt.presented == [] and log.events == []


@pytest.mark.parametrize("medium", [None, "", "   "])
def test_it_needs_a_medium(medium) -> None:
    async def scenario():
        _, handler, prompt, log = _tool()
        return await handler({"new_medium": medium, "reason": "x"}), prompt, log

    result, prompt, log = asyncio.run(scenario())
    assert result["ok"] is False and "new_medium" in result["error"]
    assert prompt.presented == [] and log.events == []


def test_text_is_tidied_and_capped() -> None:
    async def scenario():
        _, handler, prompt, _ = _tool()
        task = asyncio.ensure_future(
            handler({"new_medium": "  PBS,\n pH   7.4 " + "x" * 500, "reason": None})
        )
        await _until(lambda: prompt.presented)
        prompt.answer(True)
        await asyncio.wait_for(task, 2)
        return prompt.presented[0]

    request = asyncio.run(scenario())
    assert request.new_medium.startswith("PBS, pH 7.4 x")
    assert len(request.new_medium) == 200 and "\n" not in request.new_medium
    assert request.reason == ""


def test_a_prompt_that_cannot_be_shown_does_not_leave_the_agent_waiting() -> None:
    class Broken(FakePrompt):
        def present(self, request, resolve) -> None:
            raise RuntimeError("no window")

    async def scenario():
        _, handler, prompt, log = _tool(prompt=Broken())
        return await asyncio.wait_for(handler(dict(ARGS)), 2), prompt, log

    result, prompt, log = asyncio.run(scenario())
    assert result["ok"] is False and result["performed"] is False
    assert "could not be shown" in result["error"] and "NOT switched" in result["error"]
    assert len(prompt.withdrawn) == 1
    assert log.kinds() == ["cell_switch_requested", "cell_switch_resolved"]


def test_a_failing_log_does_not_fail_the_tool() -> None:
    def broken(kind, **fields):
        raise OSError("disk full")

    async def scenario():
        _, handler, prompt, _ = _tool(log=broken)
        task = asyncio.ensure_future(handler(dict(ARGS)))
        await _until(lambda: prompt.presented)
        prompt.answer(True)
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(scenario())["performed"] is True


# ---- the record format --------------------------------------------------------------


def test_the_default_operator_is_the_login_name(monkeypatch) -> None:
    monkeypatch.setattr(cs.getpass, "getuser", lambda: "someone")
    assert cs.default_operator() == "someone"
    monkeypatch.setattr(cs.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError()))
    assert cs.default_operator() == "unknown"


def test_the_record_carries_requested_performed_timestamp_operator_confirmation() -> None:
    request = cs.CellSwitchRequest("abc12345", "PBS", "wash", "2026-11-03T10:00:00+00:00")
    done = cs.make_record(
        request, CellSwitchOutcome(True, "op", "2026-11-03T10:01:00+00:00"), "confirmed", 60.04
    )
    assert set(done) == {"record", "request_id", "requested", "performed", "confirmation", "waited_s"}
    assert set(done["requested"]) == {"medium", "reason", "timestamp"}
    assert set(done["performed"]) == {"done", "timestamp", "operator", "actuator"}
    assert done["waited_s"] == 60.0
    stopped = cs.make_record(request, None, "stopped", 1.0)
    assert stopped["performed"] == {
        "done": False, "timestamp": None, "operator": None, "actuator": None,
    }


# ---- the system prompt ---------------------------------------------------------------


def _worker(registry, **kwargs):
    return AgentWorker(registry, api_key="sk-ant-api03-" + "Zq7" * 30, model="claude-fable-5-1", **kwargs)


def _system_text(worker) -> str:
    return worker._request_kwargs("claude-fable-5-1")["system"][0]["text"]  # noqa: SLF001


def test_the_prompt_tells_the_agent_to_ask_before_changing_solution() -> None:
    tool_def, handler, *_ = _tool()
    registry = ToolRegistry()
    registry.register(tool_def, handler)
    text = _system_text(_worker(registry))
    assert SYSTEM_PROMPT_RULE in text
    for phrase in ("request_cell_switch", "Before ANY step", "PBS, then a plating bath",
                   "performed true", "cancelled or stopped", "never call it while"):
        assert phrase in text


def test_a_registry_without_the_tool_gets_the_prompt_unchanged() -> None:
    from src.agent.agent_worker import DEFAULT_SYSTEM_PROMPT

    assert _system_text(_worker(ToolRegistry())) == DEFAULT_SYSTEM_PROMPT


def test_a_custom_prompt_is_left_alone() -> None:
    tool_def, handler, *_ = _tool()
    registry = ToolRegistry()
    registry.register(tool_def, handler)
    assert _system_text(_worker(registry, system_prompt="custom")) == "custom"


def test_the_prompt_is_the_same_string_every_turn() -> None:
    tool_def, handler, *_ = _tool()
    registry = ToolRegistry()
    registry.register(tool_def, handler)
    worker = _worker(registry)
    assert _system_text(worker) == _system_text(worker)   # prompt caching holds


# ---- end to end: a scripted model, the real worker and a real session log -----------------


class _Stream:
    def __init__(self, final) -> None:
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
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

    async def get_final_message(self):
        return self._final


class _Client:
    def __init__(self, tool_input) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)
        self._replies = [
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="Asking for the bath."),
                    SimpleNamespace(type="tool_use", id="toolu_1", name=TOOL_NAME, input=tool_input),
                ],
                stop_reason="tool_use", usage=usage,
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Done.")],
                stop_reason="end_turn", usage=usage,
            ),
        ]

    def _stream(self, **_kwargs):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self) -> None:
        return None


class _AutoPrompt(FakePrompt):
    """Answers as soon as it is shown."""

    def __init__(self, confirmed: bool) -> None:
        super().__init__()
        self._confirmed = confirmed

    def present(self, request, resolve) -> None:
        super().present(request, resolve)
        resolve(CellSwitchOutcome(self._confirmed, "op", cs.now_iso()))


@pytest.mark.parametrize("confirmed", [True, False])
def test_the_whole_exchange_is_in_the_session_log(tmp_path, confirmed) -> None:
    log = SessionLog(str(tmp_path), app_version="0", mode="mock")
    tool_def, handler = build_cell_switch_tool(
        _AutoPrompt(confirmed), log=log.note, call_on_gui=_direct
    )
    registry = ToolRegistry()
    registry.register(tool_def, handler)
    client = _Client(dict(ARGS))
    worker = _worker(registry, session_log=log, client_factory=lambda **_k: client)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        worker._loop, worker._queue = loop, queue  # noqa: SLF001
        main = asyncio.ensure_future(worker._main())  # noqa: SLF001
        queue.put_nowait("Plate the electrode.")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not worker.is_busy() and queue.empty() and log.started:
                break
        worker.request_stop()
        await asyncio.wait_for(main, timeout=5)

    asyncio.run(scenario())
    events = read_events(log.path)
    kinds = [e["event"] for e in events]
    order = ["tool_call", "cell_switch_requested", "cell_switch_resolved", "tool_result"]
    assert [k for k in kinds if k in order] == order
    resolved = next(e for e in events if e["event"] == "cell_switch_resolved")
    assert resolved["confirmation"] == ("confirmed" if confirmed else "cancelled")
    assert resolved["requested"]["medium"] == "Pt-black plating bath"
    assert resolved["performed"]["done"] is confirmed
    result = next(e for e in events if e["event"] == "tool_result")
    assert result["result"]["performed"] is confirmed
    assert result["is_error"] is (not confirmed)
