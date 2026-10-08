"""Tests for the reusable ParameterForm and expandable SequenceStepWidget.

Offscreen GUI tests covering:
  * ParameterForm renders every technique parameter and round-trips a
    seeded value, and
  * a SequenceStepWidget writes channel / repeat / delay / parameter
    edits straight back onto its embedded ``SequenceStep`` (the sequence
    carries the values), rejecting malformed channel input.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.presets import Preset  # noqa: E402
from src.data.sequence import SequenceStep  # noqa: E402
from src.gui.parameter_form import ParameterForm  # noqa: E402
from src.gui.sequence_step_widget import (  # noqa: E402
    SequenceStepWidget,
    format_channels,
    parse_channels,
)


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_parameter_form_shows_all_params_and_roundtrips(qapp) -> None:
    """The form exposes every technique param, seeded value preserved."""
    form = ParameterForm()
    form.set_config("cv", {"scan_rate": 0.2})
    params = form.get_params()

    assert params["scan_rate"] == 0.2  # seeded value applied
    # Full CV parameter set is present (not just the seeded subset).
    for name in ("e_begin", "e_vertex1", "e_step", "n_scans", "cr"):
        assert name in params


def test_parse_and_format_channels() -> None:
    """Parsing keeps order + duplicates (RE/CE pairing); format is inverse."""
    assert parse_channels("1,4") == [1, 4]
    assert parse_channels("1-4") == [1, 2, 3, 4]
    assert parse_channels("3,1,1") == [3, 1, 1]  # order + dupes preserved
    assert parse_channels("2,2,2") == [2, 2, 2]  # valid RE/CE pairing
    assert format_channels([1, 4, 7]) == "1,4,7"
    with pytest.raises(ValueError):
        parse_channels("1,x")
    # A reversed range must error, not silently parse to an empty list.
    with pytest.raises(ValueError):
        parse_channels("5-2")


def test_step_widget_rejects_empty_channel_list(qapp) -> None:
    """Blank channel text is rejected (a 0-channel step would pass eager
    validation and only fail mid-run, halting the rest of the queue)."""
    preset = Preset(name="CV", technique="cv", params={}, channels=[1, 4])
    step = SequenceStep.from_preset("cv", preset)
    widget = SequenceStepWidget(step)

    widget._channels_edit.setText("")  # noqa: SLF001
    widget._commit_channels()  # noqa: SLF001

    assert step.channels == [1, 4]  # unchanged
    assert widget._channels_edit.text() == "1,4"  # noqa: SLF001 restored


def test_step_widget_edits_write_back_to_step(qapp) -> None:
    """Channel / repeat / delay / param edits land on the embedded step."""
    preset = Preset(
        name="CV", technique="cv", params={"scan_rate": 0.1}, channels=[1, 4]
    )
    step = SequenceStep.from_preset("cv", preset)
    widget = SequenceStepWidget(step)

    widget._channels_edit.setText("2,3,5")  # noqa: SLF001
    widget._commit_channels()  # noqa: SLF001
    assert step.channels == [2, 3, 5]

    widget._repeat_spin.setValue(3)  # noqa: SLF001
    widget._delay_spin.setValue(2.0)  # noqa: SLF001
    widget._commit_repeat_delay()  # noqa: SLF001
    assert step.repeat == 3
    assert step.delay_s == 2.0

    widget._params.set_config("cv", {"scan_rate": 0.4})  # noqa: SLF001
    widget._commit_params()  # noqa: SLF001
    assert step.params["scan_rate"] == 0.4


def test_step_widget_rejects_malformed_channels(qapp) -> None:
    """Bad channel text is rejected: step unchanged, field restored."""
    preset = Preset(
        name="CV", technique="cv", params={}, channels=[1, 4]
    )
    step = SequenceStep.from_preset("cv", preset)
    widget = SequenceStepWidget(step)

    widget._channels_edit.setText("oops")  # noqa: SLF001
    widget._commit_channels()  # noqa: SLF001

    assert step.channels == [1, 4]
    assert widget._channels_edit.text() == "1,4"  # noqa: SLF001


def test_step_widget_manual_mode_reveals_re_ce_field(qapp) -> None:
    """Selecting Mode C shows the RE/CE field and writes the pairing back."""
    preset = Preset(
        name="CV", technique="cv", params={}, channels=[1, 2]
    )
    step = SequenceStep.from_preset("cv", preset)  # external by default
    widget = SequenceStepWidget(step)

    # Hidden for external/on_board.
    assert widget._re_ce_row.isHidden() is True  # noqa: SLF001

    # Switching to manual reveals the field and records the mode.
    widget._mode_combo.setCurrentText("manual")  # noqa: SLF001
    assert step.electrode_config_mode == "manual"
    assert widget._re_ce_row.isHidden() is False  # noqa: SLF001

    # The entered pairing lands on the step.
    widget._re_ce_edit.setText("3,3")  # noqa: SLF001
    widget._commit_re_ce()  # noqa: SLF001
    assert step.re_ce_channels == [3, 3]
