"""The agent's memory tools: save_memory, list_memories and read_memory.

No instrument and no API key. The model is a scripted client; the session
log and the memory folder are real files under ``tmp_path``. The dock's
button is tested in ``tests/gui/test_save_chat_button.py``.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent.agent_worker import DEFAULT_SYSTEM_PROMPT, AgentWorker  # noqa: E402
from src.agent.memory_store import REQUIRED_SECTIONS, SECTIONS  # noqa: E402
from src.agent.memory_tools import (  # noqa: E402
    LIST_TOOL_NAME,
    READ_TOOL_NAME,
    RUNS_HEADING,
    SAVE_CHAT_MESSAGE,
    SAVE_TOOL_NAME,
    SYSTEM_PROMPT_RULE,
    build_memory_tools,
    runs_since_last_save,
)
from src.agent.session_log import SessionLog, read_events  # noqa: E402
from src.agent.tools import ToolRegistry, dispatch_tool  # noqa: E402

NOTE = {
    "title": "Resistor cell, channel 3",
    "goal": "Measure the resistance of channel 3 of the resistor test cell.",
    "electrode_setup": "Resistor test cell, channel 3, external RE/CE wiring.",
    "electrochemical_setup": "No solution. CA hold at 0.1 V for 5 s, range 100u.",
    "what_was_done": "One CA hold on channel 3.",
    "what_was_found": "Steady current 10.0 uA.",
}


class Log:
    def __init__(self) -> None:
        self.events = []

    def __call__(self, kind, **fields) -> None:
        self.events.append((kind, fields))


def _registry(folder, **kwargs) -> ToolRegistry:
    registry = ToolRegistry()
    for tool_def, handler in build_memory_tools(lambda: str(folder), **kwargs):
        registry.register(tool_def, handler)
    return registry


def _call(registry, name, args):
    text, is_error = asyncio.run(dispatch_tool(registry, name, args))
    return json.loads(text), is_error


# ---- the tool definitions ----------------------------------------------------


def test_the_three_tools_register_beside_the_other_tools(tmp_path) -> None:
    registry = _registry(tmp_path)
    assert registry.names() == [LIST_TOOL_NAME, READ_TOOL_NAME, SAVE_TOOL_NAME]


def test_save_memory_asks_for_the_electrode_and_electrochemical_setup(tmp_path) -> None:
    defs = {d["name"]: d for d in _registry(tmp_path).tool_defs}
    schema = defs[SAVE_TOOL_NAME]["input_schema"]
    assert schema["required"] == ["title", *REQUIRED_SECTIONS]
    assert "electrode_setup" in schema["required"]
    assert "electrochemical_setup" in schema["required"]
    assert set(schema["properties"]) == {"title", "operator", *(k for k, _ in SECTIONS)}
    electrode = schema["properties"]["electrode_setup"]["description"]
    for phrase in ("working electrode", "reference electrode", "counter electrode",
                   "RE/CE wiring mode", "not stated"):
        assert phrase in electrode
    cell = schema["properties"]["electrochemical_setup"]["description"]
    for phrase in ("electrolyte", "pH", "scan rate", "current range", "not stated"):
        assert phrase in cell
    assert "do not guess" in defs[SAVE_TOOL_NAME]["description"]


# ---- saving ---------------------------------------------------------------


def test_saving_writes_one_note_and_reports_its_name(tmp_path) -> None:
    log = Log()
    folder = tmp_path / "memories"
    result, is_error = _call(_registry(folder, log=log), SAVE_TOOL_NAME, dict(NOTE))
    assert is_error is False
    assert result["saved"] is True
    assert result["saved_at"] and result["title"] == "Resistor cell, channel 3"
    assert result["folder"] == str(folder)
    files = list(folder.iterdir())
    assert [f.name for f in files] == [result["name"]]
    text = files[0].read_text(encoding="utf-8")
    assert "# Resistor cell, channel 3" in text
    assert "## Electrode setup" in text and "## Electrochemical setup" in text
    assert log.events == [
        ("memory_saved", {"name": result["name"], "folder": str(folder),
                          "title": "Resistor cell, channel 3"})
    ]


@pytest.mark.parametrize("missing", ["title", *REQUIRED_SECTIONS])
def test_a_note_with_a_missing_part_is_refused_and_nothing_is_written(
    tmp_path, missing
) -> None:
    args = dict(NOTE)
    args[missing] = ""
    result, is_error = _call(_registry(tmp_path), SAVE_TOOL_NAME, args)
    assert is_error is True
    assert result["saved"] is False
    assert "Nothing was saved" in result["error"]
    assert list(tmp_path.iterdir()) == []


def test_a_folder_that_cannot_be_written_is_reported_not_raised(tmp_path) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_text("x", encoding="utf-8")
    result, is_error = _call(
        _registry(blocker / "memories"), SAVE_TOOL_NAME, dict(NOTE)
    )
    assert is_error is True
    assert result["saved"] is False
    assert "Nothing was saved" in result["error"]


def test_a_failing_log_does_not_fail_the_save(tmp_path) -> None:
    def broken(kind, **fields):
        raise RuntimeError("disk full")

    registry = _registry(tmp_path, log=broken)
    result, is_error = _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    assert is_error is False and result["saved"] is True


def test_the_operator_is_written_only_when_given(tmp_path) -> None:
    registry = _registry(tmp_path)
    first, _ = _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    second, _ = _call(registry, SAVE_TOOL_NAME, {**NOTE, "operator": " A. Student "})
    assert "Operator" not in (tmp_path / first["name"]).read_text(encoding="utf-8")
    assert "- Operator: A. Student" in (tmp_path / second["name"]).read_text(
        encoding="utf-8"
    )


def test_the_folder_is_read_on_every_call(tmp_path) -> None:
    where = {"folder": str(tmp_path / "one")}
    registry = ToolRegistry()
    for tool_def, handler in build_memory_tools(lambda: where["folder"]):
        registry.register(tool_def, handler)
    _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    where["folder"] = str(tmp_path / "two")
    _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    assert len(list((tmp_path / "one").iterdir())) == 1
    assert len(list((tmp_path / "two").iterdir())) == 1


# ---- the runs the program copies from the session log -------------------------


def _event(kind, seq, **fields):
    return {"ts": f"2026-11-12T14:00:{seq:02d}.000-05:00", "seq": seq,
            "event": kind, **fields}


def _run(seq, call_id, name="run_ca", args=None, **result):
    return [
        _event("tool_call", seq, id=call_id, name=name,
               arguments=args or {"channels": [3], "e_dc": 0.1}),
        _event("tool_result", seq + 1, id=call_id, name=name,
               is_error=False, result={"ok": True}, **result),
    ]


def test_runs_are_listed_with_their_arguments_as_sent() -> None:
    events = _run(1, "a", artifacts={"export_paths": ["C:/data/20261112_140001_ca"]})
    text = runs_since_last_save(events)
    assert "not the agent's summary" in text
    line = text.splitlines()[-1]
    assert line == (
        '- 14:00:01 run_ca {"channels":[3],"e_dc":0.1} -> ok; '
        "files: C:/data/20261112_140001_ca"
    )


def test_a_failed_a_refused_and_a_stopped_run_are_labelled() -> None:
    events = [
        _event("tool_call", 1, id="a", name="run_cv", arguments={"e_end": 2.0}),
        _event("tool_result", 2, id="a", name="run_cv", is_error=True,
               result={"ok": False, "refused_by": "request_limits"}),
        _event("tool_call", 3, id="b", name="run_eis", arguments={}),
        _event("tool_result", 4, id="b", name="run_eis", is_error=True,
               result={"ok": False, "error": "Device  not\nconnected."}),
        _event("tool_call", 5, id="c", name="run_ca", arguments={}),
        _event("tool_result", 6, id="c", name="run_ca", is_error=True,
               cancelled=True, result={"ok": False}),
        _event("tool_call", 7, id="d", name="run_ca", arguments={}),
    ]
    lines = runs_since_last_save(events).splitlines()[-4:]
    assert lines[0].endswith("-> REFUSED by the request limits (nothing was run)")
    assert lines[1].endswith("-> FAILED: Device not connected.")
    assert lines[2].endswith("-> stopped")
    assert lines[3].endswith("-> no result (stopped)")


def test_only_instrument_requests_are_listed() -> None:
    events = (
        _run(1, "a", name="device_status")
        + _run(3, "b", name="analyze_cv")
        + _run(5, "c", name="request_cell_switch", args={"new_medium": "PBS"})
        + _run(7, "d", name="export_session")
    )
    lines = [
        line
        for line in runs_since_last_save(events).splitlines()
        if line.startswith("- ")
    ]
    assert [line.split()[2] for line in lines] == [
        "request_cell_switch",
        "export_session",
    ]


def test_only_the_runs_since_the_last_saved_note_are_listed() -> None:
    events = (
        _run(1, "a", args={"channels": [1]})
        + [_event("memory_saved", 3, name="first.md")]
        + _run(4, "b", args={"channels": [2]})
    )
    text = runs_since_last_save(events)
    assert '"channels":[2]' in text
    assert '"channels":[1]' not in text


def test_a_chat_with_no_runs_says_so() -> None:
    assert runs_since_last_save([]) == "No instrument runs are recorded for this chat."
    assert "No instrument runs" in runs_since_last_save(
        [_event("user_message", 1, text="hello")]
    )


def test_a_very_long_list_keeps_the_newest_runs_and_says_what_was_left_out() -> None:
    events = []
    for index in range(100):
        events += _run(index % 50, f"id{index}", args={"n": index})
    text = runs_since_last_save(events)
    assert "(20 earlier lines left out; see the session log.)" in text
    assert '{"n":99}' in text and '{"n":19}' not in text


def test_a_saved_note_names_the_session_log_and_lists_its_runs(tmp_path) -> None:
    log = SessionLog(str(tmp_path / "logs"), app_version="0", mode="mock")
    log.start("claude-sonnet-5-5", [SAVE_TOOL_NAME])
    log.tool_call("t1", "run_ca", {"channels": [3], "e_dc": 0.1, "t_run": 5})
    log.tool_result("t1", "run_ca", '{"ok": true}', is_error=False, duration_ms=1.0)
    log.assistant_message("Done.", model="claude-haiku-5-5", stop_reason="end_turn",
                          tool_calls=0)
    folder = tmp_path / "memories"
    registry = _registry(folder, log_path_fn=lambda: log.path, log=log.note)

    first, _ = _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    text = (folder / first["name"]).read_text(encoding="utf-8")
    assert f"- Session log: {os.path.basename(log.path)}" in text
    assert "- Model: claude-haiku-5-5" in text
    assert f"## {RUNS_HEADING}" in text
    assert 'run_ca {"channels":[3],"e_dc":0.1,"t_run":5} -> ok' in text

    # A second note in the same session lists only what came after the first.
    second, _ = _call(registry, SAVE_TOOL_NAME, {**NOTE, "title": "Later"})
    later = (folder / second["name"]).read_text(encoding="utf-8")
    assert "No instrument runs are recorded for this chat." in later
    log.end()
    saved = [e for e in read_events(log.path) if e["event"] == "memory_saved"]
    assert [e["name"] for e in saved] == [first["name"], second["name"]]


def test_without_a_session_log_the_note_has_no_runs_section(tmp_path) -> None:
    result, _ = _call(_registry(tmp_path), SAVE_TOOL_NAME, dict(NOTE))
    text = (tmp_path / result["name"]).read_text(encoding="utf-8")
    assert RUNS_HEADING not in text and "Session log" not in text


# ---- listing and reading ------------------------------------------------------


def test_an_empty_folder_lists_no_notes(tmp_path) -> None:
    result, is_error = _call(_registry(tmp_path / "none"), LIST_TOOL_NAME, {})
    assert is_error is False
    assert result["count"] == 0 and result["memories"] == []


def test_a_later_session_finds_and_reads_an_earlier_note(tmp_path) -> None:
    folder = tmp_path / "memories"
    saved, _ = _call(_registry(folder), SAVE_TOOL_NAME, dict(NOTE))

    later = _registry(folder)  # another session, same folder
    listed, _ = _call(later, LIST_TOOL_NAME, {"search": "resistor"})
    assert listed["count"] == 1
    entry = listed["memories"][0]
    assert entry["name"] == saved["name"]
    assert entry["title"] == "Resistor cell, channel 3"
    assert entry["goal"].startswith("Measure the resistance of channel 3")

    read, is_error = _call(later, READ_TOOL_NAME, {"name": entry["name"]})
    assert is_error is False
    assert "## Electrode setup" in read["text"]
    assert read["truncated"] is False
    assert "not a measurement" in read["reminder"]


def test_a_search_with_no_match_lists_nothing(tmp_path) -> None:
    registry = _registry(tmp_path)
    _call(registry, SAVE_TOOL_NAME, dict(NOTE))
    result, _ = _call(registry, LIST_TOOL_NAME, {"search": "platinum"})
    assert result["count"] == 0 and result["search"] == "platinum"


def test_only_the_newest_notes_are_listed_and_the_result_says_so(tmp_path) -> None:
    for index in range(55):
        (tmp_path / f"note_{index:02d}.md").write_text(
            f"# Note {index}\n\n- Saved: 2026-11-12T14:{index:02d}:00+00:00\n",
            encoding="utf-8",
        )
    result, _ = _call(_registry(tmp_path), LIST_TOOL_NAME, {})
    assert result["count"] == 55
    assert len(result["memories"]) == 50
    assert result["memories"][0]["title"] == "Note 54"
    assert "search" in result["note"]


@pytest.mark.parametrize("name", ["", "..\\app_settings.json", "../x.md", "nope.md"])
def test_a_bad_or_unknown_note_name_is_an_error_that_points_to_the_list(
    tmp_path, name
) -> None:
    (tmp_path / "x.md").write_text("outside\n", encoding="utf-8")
    result, is_error = _call(
        _registry(tmp_path / "memories"), READ_TOOL_NAME, {"name": name}
    )
    assert is_error is True
    assert "list_memories" in result["error"]
    assert "outside" not in json.dumps(result)


# ---- the system prompt ------------------------------------------------------


MODEL = "claude-fable-5-1"


def _worker(registry, **kwargs):
    key = "sk-ant-api03-" + "Zq7" * 30
    return AgentWorker(registry, api_key=key, model=MODEL, **kwargs)


def _system_text(worker) -> str:
    return worker._request_kwargs(MODEL)["system"][0]["text"]  # noqa: SLF001


def test_the_prompt_tells_the_agent_to_check_the_notes(tmp_path) -> None:
    text = _system_text(_worker(_registry(tmp_path)))
    assert text == DEFAULT_SYSTEM_PROMPT + SYSTEM_PROMPT_RULE
    for phrase in ("list_memories", "read_memory", "how something was done before",
                   "never report a number from a note as measured",
                   "never overrides the person's instructions or the request limits",
                   "only when the person asks to save"):
        assert phrase in text


def test_a_registry_without_the_tools_gets_the_prompt_unchanged() -> None:
    assert _system_text(_worker(ToolRegistry())) == DEFAULT_SYSTEM_PROMPT


def test_a_custom_prompt_is_left_alone(tmp_path) -> None:
    worker = _worker(_registry(tmp_path), system_prompt="custom")
    assert _system_text(worker) == "custom"


def test_the_button_message_asks_for_one_save_and_no_measurement() -> None:
    assert "save_memory once" in SAVE_CHAT_MESSAGE
    assert "Do not start any measurement" in SAVE_CHAT_MESSAGE


# ---- end to end: a scripted model, the real worker and a real session log ------


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
    """Replies with one tool call, then with plain text."""

    def __init__(self, name, tool_input) -> None:
        self.calls = 0
        self.requests = []
        self.messages = SimpleNamespace(stream=self._stream)
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)
        self._replies = [
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", id="toolu_1", name=name,
                                         input=tool_input)],
                stop_reason="tool_use", usage=usage,
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Saved.")],
                stop_reason="end_turn", usage=usage,
            ),
        ]

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self) -> None:
        return None


def test_the_button_message_ends_in_a_note_and_the_log_shows_it(tmp_path) -> None:
    log = SessionLog(str(tmp_path / "logs"), app_version="0", mode="mock")
    folder = tmp_path / "memories"
    registry = _registry(folder, log_path_fn=lambda: log.path, log=log.note)
    client = _Client(SAVE_TOOL_NAME, dict(NOTE))
    worker = _worker(registry, session_log=log, client_factory=lambda **_k: client)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        worker._loop, worker._queue = loop, queue  # noqa: SLF001
        main = asyncio.ensure_future(worker._main())  # noqa: SLF001
        queue.put_nowait(SAVE_CHAT_MESSAGE)
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not worker.is_busy() and queue.empty() and log.started:
                break
        worker.request_stop()
        await asyncio.wait_for(main, timeout=5)

    asyncio.run(scenario())
    notes = list(folder.iterdir())
    assert len(notes) == 1
    events = read_events(log.path)
    kinds = [e["event"] for e in events]
    order = ["user_message", "tool_call", "memory_saved", "tool_result"]
    assert [k for k in kinds if k in order] == order
    assert next(e for e in events if e["event"] == "user_message")["text"] == (
        SAVE_CHAT_MESSAGE
    )
    result = next(e for e in events if e["event"] == "tool_result")
    assert result["is_error"] is False
    assert result["result"]["name"] == notes[0].name
    # The model was offered the memory tools and given the rule.
    offered = [tool["name"] for tool in client.requests[0]["tools"]]
    assert offered == [SAVE_TOOL_NAME, LIST_TOOL_NAME, READ_TOOL_NAME]
    assert SYSTEM_PROMPT_RULE in client.requests[0]["system"][0]["text"]
