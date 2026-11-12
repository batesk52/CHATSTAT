"""The agent picks an electrode setup by name.

The run tools (and save_preset / save_sequence) take an optional
``electrode_setup``. These tests check the rules:

1. a call with the older arguments sends the same script as before;
2. a setup name gives the setup's wiring, ``channels`` may narrow it, and
   conflicting explicit wiring is refused;
3. an unknown name is refused with the names that exist;
4. device_status lists the setups;
5. the tool descriptions and the system prompt describe setups and
   per-electrode RE/CE, not modes;
6. the wiring rules and the request limits still apply.

The configuration a run would use is captured where the adapter builds it
(``build_technique_config``), so nothing reaches an engine.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

import src.agent.engine_adapter as engine_adapter_module
from src.agent.agent_worker import DEFAULT_SYSTEM_PROMPT
from src.agent.engine_adapter import EngineAdapter
from src.agent.preset_tools import build_preset_tools
from src.agent.request_limits import RequestLimits
from src.agent.tools import MEASUREMENT_TECHNIQUES, build_registry, build_tool_defs
from src.data.electrode_presets import ElectrodePreset, builtin_electrode_presets
from src.data.models import TechniqueConfig
from src.data.presets import read_preset_file
from src.data.sequence import Sequence
from src.techniques.scripts import generate, technique_params

BUILT_IN_KEYS = sorted(builtin_electrode_presets())


class _Captured(Exception):
    pass


@pytest.fixture()
def captured(monkeypatch):
    """Record the config each run would use, then stop before the engine."""
    seen: list[TechniqueConfig] = []
    real = engine_adapter_module.build_technique_config

    def spy(technique, args=None):
        config = real(technique, args)
        seen.append(config)
        raise ValueError("captured")

    monkeypatch.setattr(engine_adapter_module, "build_technique_config", spy)
    return seen


def _registry(setups=None, limits=None):
    engine = SimpleNamespace(isRunning=lambda: False)
    adapter = EngineAdapter(
        engine,
        SimpleNamespace(is_connected=False),
        limits=limits or (lambda: RequestLimits()),
    )
    if setups is None:
        return build_registry(adapter)
    return build_registry(adapter, setups=lambda: setups)


def _call(registry, name, tool_input):
    return asyncio.run(registry.get(name)(tool_input))


def _script(config: TechniqueConfig) -> list[str]:
    return generate(
        config.technique,
        dict(config.params),
        list(config.channels),
        re_ce_channels=list(config.re_ce_channels),
    )


def _one(captured) -> TechniqueConfig:
    assert len(captured) == 1, captured
    config = captured.pop()
    return config


# ---- 1. older calls are unchanged --------------------------------------------

OLD_CALLS = [
    # (tool input, channels, RE/CE and internal label the old code used).
    # A request with no label has always run under 'external', whatever
    # re_ce_channels says; that is kept, so the label exported with the
    # data and the requests that run are those of the previous version.
    ({"channels": [1]}, [1], [15], "external"),
    ({"channels": [3, 1, 16]}, [3, 1, 16], [15, 15, 15], "external"),
    ({"channels": [2, 5], "electrode_config_mode": "external"}, [2, 5], [15, 15], "external"),
    ({"channels": [2, 5], "electrode_config_mode": "on_board"}, [2, 5], [16, 16], "on_board"),
    (
        {"channels": [2, 3], "electrode_config_mode": "manual", "re_ce_channels": [5, 6]},
        [2, 3],
        [5, 6],
        "manual",
    ),
    (
        {"channels": [1, 2], "electrode_config_mode": "manual", "re_ce_channels": [1, 2]},
        [1, 2],
        [1, 2],
        "manual",
    ),
    ({"channels": [4], "re_ce_channels": [4]}, [4], [4], "external"),
    ({"channels": [1, 2], "re_ce_channels": [16, 16]}, [1, 2], [16, 16], "external"),
    # Mixed tables with no label: accepted by the previous version (the
    # same-position wiring check in one run), so still accepted.
    ({"channels": [3, 15], "re_ce_channels": [3, 15]}, [3, 15], [3, 15], "external"),
    (
        {"channels": list(range(1, 17)), "re_ce_channels": list(range(1, 17))},
        list(range(1, 17)),
        list(range(1, 17)),
        "external",
    ),
]


@pytest.mark.parametrize("technique", MEASUREMENT_TECHNIQUES)
@pytest.mark.parametrize("tool_input, channels, re_ce, label", OLD_CALLS)
def test_an_older_call_sends_the_same_script(
    captured, technique, tool_input, channels, re_ce, label
) -> None:
    result = _call(_registry(), f"run_{technique}", dict(tool_input))
    assert result["ok"] is False and result["error"] == "captured"
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == (channels, re_ce)
    assert config.electrode_config_mode == label
    expected = generate(technique, technique_params(technique), channels, re_ce_channels=re_ce)
    assert _script(config) == expected


# ---- 2. a setup name gives the setup's wiring ---------------------------------


@pytest.mark.parametrize("technique", MEASUREMENT_TECHNIQUES)
@pytest.mark.parametrize("key", BUILT_IN_KEYS)
def test_a_setup_name_sends_the_same_script_as_its_wiring(captured, key, technique) -> None:
    preset = builtin_electrode_presets()[key]
    mode, channels, re_ce = preset.wiring()
    registry = _registry()
    _call(registry, f"run_{technique}", {"electrode_setup": key})
    by_name = _one(captured)
    _call(
        registry,
        f"run_{technique}",
        {"channels": channels, "electrode_config_mode": mode, "re_ce_channels": re_ce},
    )
    by_args = _one(captured)
    assert (by_name.electrode_config_mode, by_name.channels, by_name.re_ce_channels) == (
        by_args.electrode_config_mode,
        by_args.channels,
        by_args.re_ce_channels,
    )
    assert _script(by_name) == _script(by_args)
    # And the older form with no RE/CE list for a shared position.
    if mode != "manual":
        _call(
            registry,
            f"run_{technique}",
            {"channels": channels, "electrode_config_mode": mode},
        )
        assert _script(_one(captured)) == _script(by_name)


def test_the_display_name_works_too_ignoring_case(captured) -> None:
    name = builtin_electrode_presets()["on_chip_re_ce"].name
    _call(_registry(), "run_ca", {"electrode_setup": "  " + name.upper() + " "})
    config = _one(captured)
    assert config.channels == list(range(1, 17))
    assert config.re_ce_channels == [16] * 16
    assert config.electrode_config_mode == "on_board"


@pytest.mark.parametrize(
    "key, channels, re_ce",
    [
        ("per_electrode_re_ce", [3, 5], [3, 5]),
        ("per_electrode_re_ce", [9, 2], [9, 2]),
        ("separate_re_ce", [16, 2], [15, 15]),
        ("die_16_on_chip", [7], [16]),
    ],
)
def test_channels_narrow_a_setup(captured, key, channels, re_ce) -> None:
    _call(_registry(), "run_ca", {"electrode_setup": key, "channels": channels})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == (channels, re_ce)


def test_a_channel_the_setup_lacks_is_refused(captured) -> None:
    result = _call(
        _registry(), "run_ca", {"electrode_setup": "per_electrode_re_ce", "channels": [15]}
    )
    assert result["ok"] is False
    assert "[15]" in result["error"] and "per_electrode_re_ce" in result["error"]
    assert captured == []


@pytest.mark.parametrize(
    "extra, named",
    [
        ({"re_ce_channels": [15]}, "re_ce_channels"),
        ({"electrode_config_mode": "on_board"}, "electrode_config_mode"),
        (
            {"re_ce_channels": [1], "electrode_config_mode": "manual"},
            "re_ce_channels, electrode_config_mode",
        ),
    ],
)
def test_a_setup_with_explicit_wiring_is_refused(captured, extra, named) -> None:
    tool_input = {"electrode_setup": "separate_re_ce", "channels": [1], **extra}
    result = _call(_registry(), "run_cv", tool_input)
    assert result["ok"] is False
    assert f"Give either electrode_setup or {named}, not both" in result["error"]
    assert "Drop" in result["error"]
    assert captured == []


# ---- 3. an unknown name lists the names --------------------------------------


def test_an_unknown_setup_lists_the_available_names(captured) -> None:
    result = _call(_registry(), "run_eis", {"electrode_setup": "mode c"})
    assert result["ok"] is False
    assert "Unknown electrode setup 'mode c'" in result["error"]
    for key, preset in builtin_electrode_presets().items():
        assert repr(key) in result["error"]
        assert preset.name in result["error"]
    assert captured == []


def test_neither_channels_nor_a_setup_is_refused(captured) -> None:
    result = _call(_registry(), "run_ca", {"e_dc": 0.1})
    assert result["ok"] is False
    assert "channels" in result["error"] and "electrode_setup" in result["error"]
    assert captured == []


# ---- 4. device_status lists the setups, including saved ones -----------------


def test_device_status_lists_the_built_in_setups() -> None:
    adapter = EngineAdapter(
        SimpleNamespace(isRunning=lambda: False),
        SimpleNamespace(is_connected=False),
    )
    status = asyncio.run(build_registry(adapter).get("device_status")({}))
    assert status["ok"] is True
    listed = {entry["key"]: entry for entry in status["electrode_setups"]}
    assert sorted(listed) == BUILT_IN_KEYS
    assert "position 15" in listed["separate_re_ce"]["wiring"]


def test_a_saved_setup_is_listed_and_can_be_run_by_name(captured) -> None:
    setups = builtin_electrode_presets()
    setups["bench_pairs"] = ElectrodePreset(
        name="Bench pairs", channels=[2, 3], re_ce_channels=[5, 6]
    )
    registry = _registry(setups)
    status = asyncio.run(registry.get("device_status")({}))
    assert "bench_pairs" in {entry["key"] for entry in status["electrode_setups"]}
    _call(registry, "run_ca", {"electrode_setup": "Bench pairs"})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == ([2, 3], [5, 6])
    assert config.electrode_config_mode == "manual"


# ---- 5. no modes in what the model reads -------------------------------------

_MODE_WORDS = re.compile(r"\bmode[ -]?[abc]\b", re.IGNORECASE)


def _all_descriptions(obj) -> list[str]:
    if isinstance(obj, dict):
        out = [obj["description"]] if isinstance(obj.get("description"), str) else []
        for value in obj.values():
            out.extend(_all_descriptions(value))
        return out
    if isinstance(obj, list):
        return [text for item in obj for text in _all_descriptions(item)]
    return []


def test_no_tool_or_prompt_text_talks_about_wiring_modes() -> None:
    defs = build_tool_defs() + [d for d, _ in build_preset_tools()]
    texts = _all_descriptions(defs) + [DEFAULT_SYSTEM_PROMPT]
    for text in texts:
        assert not _MODE_WORDS.search(text), text
    for label in ("'external'", "'on_board'", "'manual'"):
        assert label not in DEFAULT_SYSTEM_PROMPT
    assert "electrode_setup" in DEFAULT_SYSTEM_PROMPT
    assert "re_ce_channels" in DEFAULT_SYSTEM_PROMPT
    for definition in build_tool_defs():
        if definition["name"].startswith("run_"):
            props = definition["input_schema"]["properties"]
            assert "electrode_setup" in props
            assert "leave it out" in props["electrode_config_mode"]["description"]


def test_the_wiring_check_is_still_in_the_prompt() -> None:
    for phrase in (
        "6. Wiring check.",
        "suspect the wiring before the current range and before the cell",
        "never report a resistance computed from it",
        "re-measure the SAME channels (and only those)",
        "re_ce_channels equal to channels",
        "Report a channel as open or miswired only if it still passes no current",
        "short CA hold",
        "BEFORE any long measurement",
        "no value is always better than a wrong value",
    ):
        assert phrase in DEFAULT_SYSTEM_PROMPT, phrase


# ---- 6. the rules and the limits still hold ----------------------------------


def test_a_per_electrode_setup_gets_the_per_electrode_label(captured) -> None:
    """A setup's label follows its table, as in the GUI."""
    _call(_registry(), "run_ca", {"electrode_setup": "per_electrode_re_ce", "channels": [3]})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == ([3], [3])
    assert config.electrode_config_mode == "manual"


def test_the_per_electrode_rules_still_refuse_15_and_16() -> None:
    result = _call(
        _registry(),
        "run_ca",
        {"channels": [3, 15], "re_ce_channels": [3, 15], "electrode_config_mode": "manual"},
    )
    assert result["ok"] is False
    assert "1-14" in result["error"]


# ---- names two setups share ---------------------------------------------------


def test_a_display_name_two_setups_share_is_refused(captured) -> None:
    """A saved setup that reuses a built-in's display name under its own key."""
    setups = builtin_electrode_presets()
    shared = setups["on_chip_re_ce"].name
    setups["reference_counter_on_the_chip_shorted"] = ElectrodePreset(
        name=shared, channels=[1, 2], re_ce_channels=[5, 6]
    )
    registry = _registry(setups)
    result = _call(registry, "run_ca", {"electrode_setup": shared})
    assert result["ok"] is False
    assert "More than one electrode setup is called" in result["error"]
    assert "'on_chip_re_ce'" in result["error"]
    assert "'reference_counter_on_the_chip_shorted'" in result["error"]
    assert captured == []
    # Each is still reachable by its key, with its own wiring.
    _call(registry, "run_ca", {"electrode_setup": "reference_counter_on_the_chip_shorted"})
    saved = _one(captured)
    assert (saved.channels, saved.re_ce_channels) == ([1, 2], [5, 6])
    _call(registry, "run_ca", {"electrode_setup": "on_chip_re_ce"})
    built_in = _one(captured)
    assert (built_in.channels, built_in.re_ce_channels) == (list(range(1, 17)), [16] * 16)


def test_a_key_wins_over_another_setups_display_name(captured) -> None:
    """A saved setup whose display name is another setup's key."""
    setups = builtin_electrode_presets()
    setups["my_pairs"] = ElectrodePreset(
        name="separate_re_ce", channels=[2, 3], re_ce_channels=[5, 6]
    )
    registry = _registry(setups)
    _call(registry, "run_ca", {"electrode_setup": "separate_re_ce"})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == (list(range(1, 17)), [15] * 16)
    _call(registry, "run_ca", {"electrode_setup": "my_pairs"})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == ([2, 3], [5, 6])


def test_a_setup_name_is_held_to_the_request_limits() -> None:
    started: list = []
    engine = SimpleNamespace(
        isRunning=lambda: False, start_measurement=lambda *a: started.append(a)
    )
    adapter = EngineAdapter(
        engine, None, limits=lambda: RequestLimits(max_abs_potential_v=0.1)
    )
    registry = build_registry(adapter)
    result = _call(
        registry, "run_ca", {"electrode_setup": "separate_re_ce", "e_dc": 0.5}
    )
    assert result["ok"] is False
    assert result["refused_by"] == "request_limits"
    assert started == []


# ---- save_preset and save_sequence take a setup name too ---------------------


def test_save_preset_keeps_a_per_electrode_list_given_without_a_label(tmp_path) -> None:
    """channels [4] with re_ce_channels [4] ran at RE/CE 4; the preset keeps 4."""
    tools = {d["name"]: h for d, h in build_preset_tools()}
    path = str(tmp_path / "p.mux16")
    result = asyncio.run(
        tools["save_preset"](
            {
                "name": "four",
                "technique": "ca",
                "params": {},
                "channels": [4],
                "re_ce_channels": [4],
                "path": path,
            }
        )
    )
    assert result["ok"] is True, result
    preset = read_preset_file(path)["four"]
    assert preset.electrode_config_mode == "external"
    assert preset.re_ce_channels == [4]
    config = TechniqueConfig(
        technique="ca",
        params={},
        channels=preset.channels,
        re_ce_channels=preset.re_ce_channels,
        electrode_config_mode=preset.electrode_config_mode,
    )
    assert config.re_ce_channels == [4]


def test_save_preset_writes_the_setups_wiring(tmp_path) -> None:
    tools = {d["name"]: h for d, h in build_preset_tools()}
    path = str(tmp_path / "p.mux16")
    result = asyncio.run(
        tools["save_preset"](
            {
                "name": "pairs ca",
                "technique": "ca",
                "params": {"e_dc": 0.1},
                "electrode_setup": "per_electrode_re_ce",
                "channels": [1, 2],
                "path": path,
            }
        )
    )
    assert result["ok"] is True, result
    preset = read_preset_file(path, builtin_electrode_presets())["pairs_ca"]
    assert preset.channels == [1, 2]
    assert preset.re_ce_channels == [1, 2]
    assert preset.electrode_config_mode == "manual"
    # The preset refers to the setup by its key.
    assert preset.electrode_setup == "per_electrode_re_ce"


def test_save_sequence_steps_take_a_setup_name(tmp_path) -> None:
    tools = {d["name"]: h for d, h in build_preset_tools()}
    path = str(tmp_path / "s.mux16seq")
    result = asyncio.run(
        tools["save_sequence"](
            {
                "name": "two",
                "steps": [
                    {"technique": "ca", "params": {}, "electrode_setup": "on_chip_re_ce"},
                    {"technique": "cv", "params": {}, "channels": [4]},
                ],
                "path": path,
            }
        )
    )
    assert result["ok"] is True, result
    first, second = Sequence.load_from_path(path, builtin_electrode_presets()).steps
    assert first.channels == list(range(1, 17))
    assert first.electrode_config_mode == "on_board"
    assert first.electrode_setup == "on_chip_re_ce"
    assert second.channels == [4] and second.electrode_config_mode == "external"
    assert second.electrode_setup == ""


def test_load_tools_report_the_setup_name_and_refuse_a_missing_one(tmp_path) -> None:
    setups = builtin_electrode_presets()
    setups["bench_pairs"] = ElectrodePreset(
        name="Bench pairs", channels=[2, 3], re_ce_channels=[5, 6]
    )
    tools = {d["name"]: h for d, h in build_preset_tools(setups=lambda: setups)}
    path = str(tmp_path / "b.mux16")
    saved = asyncio.run(
        tools["save_preset"](
            {"name": "b", "technique": "ca", "params": {},
             "electrode_setup": "Bench pairs", "path": path}
        )
    )
    assert saved["ok"] is True, saved
    loaded = asyncio.run(tools["load_preset"]({"path": path}))
    (entry,) = loaded["presets"]
    assert entry["electrode_setup"] == "bench_pairs"
    assert (entry["channels"], entry["re_ce_channels"]) == ([2, 3], [5, 6])

    del setups["bench_pairs"]
    missing = asyncio.run(tools["load_preset"]({"path": path}))
    assert missing["ok"] is False
    assert "Unknown electrode setup 'bench_pairs'" in missing["error"]


def test_save_preset_refuses_a_setup_with_explicit_wiring(tmp_path) -> None:
    tools = {d["name"]: h for d, h in build_preset_tools()}
    result = asyncio.run(
        tools["save_preset"](
            {
                "name": "x",
                "technique": "ca",
                "params": {},
                "electrode_setup": "on_chip_re_ce",
                "re_ce_channels": [15],
                "channels": [1],
                "path": str(tmp_path / "x.mux16"),
            }
        )
    )
    assert result["ok"] is False
    assert "not both" in result["error"]


# ---- review follow-ups --------------------------------------------------------


@pytest.mark.parametrize("empty", [None, "", "  "])
@pytest.mark.parametrize("tool_input", [{"channels": [1]}, {"channels": [2, 3], "re_ce_channels": [5, 6], "electrode_config_mode": "manual"}])
def test_a_null_or_empty_setup_counts_as_absent(captured, empty, tool_input) -> None:
    """Models often send null for an optional argument."""
    _call(_registry(), "run_ca", dict(tool_input))
    without = _one(captured)
    _call(_registry(), "run_ca", {**tool_input, "electrode_setup": empty})
    with_null = _one(captured)
    assert (with_null.channels, with_null.re_ce_channels, with_null.electrode_config_mode) == (
        without.channels,
        without.re_ce_channels,
        without.electrode_config_mode,
    )
    assert _script(with_null) == _script(without)


def test_a_non_string_setup_is_still_refused(captured) -> None:
    result = _call(_registry(), "run_ca", {"electrode_setup": 3, "channels": [1]})
    assert result["ok"] is False and "must be a setup name" in result["error"]
    assert captured == []


def test_an_unsorted_saved_setup_runs_in_the_guis_order(captured) -> None:
    """A hand-edited setup listing its channels out of order runs them in
    ascending WE order, as Start in the GUI does (selected_pairs sorts),
    each keeping its own RE/CE; so the same setup name gives the same
    script in the GUI and the agent.
    """
    setups = builtin_electrode_presets()
    setups["unsorted"] = ElectrodePreset(
        name="Unsorted", channels=[7, 2, 1], re_ce_channels=[5, 6, 7]
    )
    _call(_registry(setups), "run_ca", {"electrode_setup": "unsorted"})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == ([1, 2, 7], [7, 6, 5])
    # An explicit channels list is still run in the order given.
    _call(_registry(setups), "run_ca", {"electrode_setup": "unsorted", "channels": [7, 1]})
    config = _one(captured)
    assert (config.channels, config.re_ce_channels) == ([7, 1], [5, 7])
