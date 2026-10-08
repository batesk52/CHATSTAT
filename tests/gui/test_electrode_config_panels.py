"""Tests for the new electrode-config GUI panels.

Covers :class:`src.gui.controls.ElectrodeConfigPanel` (radio selector +
mode_changed signal) and :class:`src.gui.controls.ManualChannelPanel`
(14-row table, bulk-set buttons, pairs_changed signal).
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots in headless CI / WSL.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.models import (  # noqa: E402
    EXTERNAL_RE_CE_CHANNEL,
    MODE_C_MAX_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
)
from src.gui.controls import (  # noqa: E402
    ElectrodeConfigPanel,
    ManualChannelPanel,
)


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all GUI tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# ElectrodeConfigPanel
# ---------------------------------------------------------------------------


def test_electrode_config_panel_defaults_to_external(qapp) -> None:
    """Default selection on construction is 'external' (Mode A)."""
    panel = ElectrodeConfigPanel()
    assert panel.selected_mode() == "external"


def test_electrode_config_panel_emits_mode_changed(qapp) -> None:
    """mode_changed fires once per user action with the new mode string."""
    panel = ElectrodeConfigPanel()

    captured: list[str] = []
    panel.mode_changed.connect(captured.append)

    panel.set_mode("on_board")
    panel.set_mode("manual")
    panel.set_mode("external")

    # Each set_mode toggles two radios (un-check old + check new) but
    # we connect to toggled(True)-only so we expect one emission each.
    assert captured == ["on_board", "manual", "external"]


def test_electrode_config_panel_ignores_unknown_mode(qapp) -> None:
    """set_mode('garbage') is a no-op."""
    panel = ElectrodeConfigPanel()
    panel.set_mode("garbage")
    # Default should be preserved.
    assert panel.selected_mode() == "external"


# ---------------------------------------------------------------------------
# ManualChannelPanel
# ---------------------------------------------------------------------------


def test_manual_panel_has_14_rows(qapp) -> None:
    """ManualChannelPanel exposes exactly MODE_C_MAX_CHANNEL rows."""
    panel = ManualChannelPanel()
    assert len(panel._enable_boxes) == MODE_C_MAX_CHANNEL
    assert len(panel._re_ce_combos) == MODE_C_MAX_CHANNEL


def test_manual_panel_default_selection_is_ch1_only(qapp) -> None:
    """Default state has CH1 enabled with RE/CE=CH1 (matches ChannelPanel)."""
    panel = ManualChannelPanel()
    we, re_ce = panel.selected_pairs()
    assert we == [1]
    assert re_ce == [1]


def test_manual_panel_re_ce_options_exclude_ch15_ch16(qapp) -> None:
    """RE/CE comboboxes offer CH1..CH14 only (no CH15 / CH16)."""
    panel = ManualChannelPanel()
    combo = panel._re_ce_combos[0]
    options = [combo.itemData(i) for i in range(combo.count())]
    assert options == list(range(1, MODE_C_MAX_CHANNEL + 1))
    assert 15 not in options
    assert 16 not in options


def test_manual_panel_apply_same_position(qapp) -> None:
    """`Apply same-position` button sets RE/CE = WE channel per enabled row."""
    panel = ManualChannelPanel()
    # Enable CH1, CH3, CH7
    panel.set_pairs([1, 3, 7], [1, 1, 1])

    # Behavior-driven: exercise the actual button click path so a broken
    # button.clicked -> slot wiring would be caught here.
    panel._apply_same_btn.click()
    we, re_ce = panel.selected_pairs()
    assert we == [1, 3, 7]
    assert re_ce == [1, 3, 7]


def test_manual_panel_apply_uniform_re_ce(qapp) -> None:
    """`Apply CH13 to all` button maps every enabled row to RE/CE=13."""
    panel = ManualChannelPanel()
    panel.set_pairs([2, 4, 6], [2, 4, 6])

    panel._apply_ch13_btn.click()
    we, re_ce = panel.selected_pairs()
    assert we == [2, 4, 6]
    assert re_ce == [13, 13, 13]


def test_manual_panel_apply_ch1_to_all(qapp) -> None:
    """`Apply CH1 to all` button maps every enabled row to RE/CE=1."""
    panel = ManualChannelPanel()
    panel.set_pairs([2, 4, 6], [5, 7, 9])

    panel._apply_ch1_btn.click()
    we, re_ce = panel.selected_pairs()
    assert we == [2, 4, 6]
    assert re_ce == [1, 1, 1]


def test_manual_panel_uniform_re_ce_rejects_out_of_range(qapp) -> None:
    """Bulk-set with CH15 (out of Mode C range) is a no-op.

    No button maps to CH15 (only the in-range Apply CH1 / Apply CH13
    buttons exist), so this test exercises the helper directly to pin
    the validation guard.
    """
    panel = ManualChannelPanel()
    panel.set_pairs([1, 2], [1, 1])

    panel._apply_uniform_re_ce(15)
    we, re_ce = panel.selected_pairs()
    assert re_ce == [1, 1]  # unchanged


def test_manual_panel_pairs_changed_signal(qapp) -> None:
    """pairs_changed fires with (we, re_ce) when state changes."""
    panel = ManualChannelPanel()

    captured: list[tuple[list[int], list[int]]] = []
    panel.pairs_changed.connect(
        lambda we, re_ce: captured.append((list(we), list(re_ce)))
    )

    panel.set_pairs([5, 9], [5, 9])
    # Last emission should reflect the new pairs.
    assert captured[-1] == ([5, 9], [5, 9])


def test_manual_panel_set_pairs_overwrites_enable_state(qapp) -> None:
    """set_pairs unchecks rows not in the new WE list."""
    panel = ManualChannelPanel()
    panel.set_pairs([1, 2, 3], [1, 2, 3])
    panel.set_pairs([5], [5])

    we, re_ce = panel.selected_pairs()
    assert we == [5]
    assert re_ce == [5]


def test_manual_panel_lengths_always_match(qapp) -> None:
    """The (we, re_ce) length-match invariant holds for arbitrary selections."""
    panel = ManualChannelPanel()
    panel.set_pairs(
        list(range(1, MODE_C_MAX_CHANNEL + 1)),
        list(range(1, MODE_C_MAX_CHANNEL + 1)),
    )
    we, re_ce = panel.selected_pairs()
    assert len(we) == len(re_ce) == MODE_C_MAX_CHANNEL
