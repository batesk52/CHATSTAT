"""A losing start must not clear the winner's agent-run mark.

Review round 1, finding 2: two agent-side callers (two MCP clients, or one
and the dock) can both pass the busy pre-check. The GUI starts the first
(mark set); the second's ``start_measurement`` raises. The mark must stay
set, or the first run would finish down the person's path with its modal
prompts. A fake engine plays the sequence on the GUI-thread helper.
"""

from __future__ import annotations

import pytest

from src.agent.engine_adapter import EngineAdapter


class _Engine:
    def __init__(self) -> None:
        self.running = False

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self.running

    def start_measurement(self, connection, config) -> None:
        if self.running:
            raise RuntimeError("MeasurementEngine is already running.")
        self.running = True


def test_the_losing_start_keeps_the_winners_mark() -> None:
    adapter = EngineAdapter(_Engine(), object())
    adapter._start_agent_run(object())  # noqa: SLF001 - the winner
    assert adapter._agent_run_active is True  # noqa: SLF001
    with pytest.raises(RuntimeError):
        adapter._start_agent_run(object())  # noqa: SLF001 - the loser
    assert adapter._agent_run_active is True  # noqa: SLF001
    assert adapter.consume_agent_run() is True


def test_a_failed_start_with_no_run_leaves_no_mark() -> None:
    engine = _Engine()
    engine.running = True            # e.g. the person's own run, not an agent's
    adapter = EngineAdapter(engine, object())
    with pytest.raises(RuntimeError):
        adapter._start_agent_run(object())  # noqa: SLF001
    assert adapter._agent_run_active is False  # noqa: SLF001
