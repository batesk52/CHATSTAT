"""Tests for the electrode-config GUI panels.

Covers :class:`src.gui.controls.ElectrodeConfigPanel` (the Setup row, with
no wiring-mode choice) and :class:`src.gui.controls.ElectrodeWiringPanel`
(the one wiring model: 16 rows, each with its own RE/CE position).
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots in headless CI / WSL.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QRadioButton,
)

from src.data.models import (  # noqa: E402
    EXTERNAL_RE_CE_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
    wiring_mode_for,
)
from src.gui.controls import (  # noqa: E402
    ElectrodeConfigPanel,
    ElectrodeWiringPanel,
)


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all GUI tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# ElectrodeConfigPanel: a setup is picked by name, no mode is chosen
# ---------------------------------------------------------------------------


def test_the_electrode_panel_has_no_mode_choice(qapp) -> None:
    panel = ElectrodeConfigPanel()
    assert panel.findChildren(QRadioButton) == []
    assert not hasattr(panel, "selected_mode")
    assert not hasattr(panel, "mode_changed")


def test_no_visible_text_names_a_mode(qapp) -> None:
    for widget in (ElectrodeConfigPanel(), ElectrodeWiringPanel()):
        texts = [widget.title(), widget.toolTip()]
        for child in widget.findChildren(QAbstractButton):
            texts += [child.text(), child.toolTip()]
        joined = " ".join(texts).lower()
        for word in ("mode a", "mode b", "mode c", "manual"):
            assert word not in joined


# ---------------------------------------------------------------------------
# ElectrodeWiringPanel
# ---------------------------------------------------------------------------


def test_the_table_has_16_rows_and_every_re_ce_position(qapp) -> None:
    panel = ElectrodeWiringPanel()
    assert len(panel._enable_boxes) == 16
    assert len(panel._re_ce_combos) == 16
    combo = panel._re_ce_combos[0]
    assert [combo.itemData(i) for i in range(combo.count())] == list(range(1, 17))


def test_the_default_is_ch1_at_position_15(qapp) -> None:
    """The start-up wiring is the one the application always started with."""
    panel = ElectrodeWiringPanel()
    we, re_ce = panel.selected_pairs()
    assert (we, re_ce) == ([1], [EXTERNAL_RE_CE_CHANNEL])
    assert wiring_mode_for(re_ce) == "external"


def test_own_position_button(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([1, 3, 7], [1, 1, 1])
    panel._apply_same_btn.click()
    assert panel.selected_pairs() == ([1, 3, 7], [1, 3, 7])


def test_own_position_leaves_ch15_and_ch16_alone(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([2, 15, 16], [15, 15, 15])
    panel._apply_same_btn.click()
    assert panel.selected_pairs() == ([2, 15, 16], [2, 15, 15])


@pytest.mark.parametrize("target", [1, 13, EXTERNAL_RE_CE_CHANNEL, ON_BOARD_RE_CE_CHANNEL])
def test_all_to_one_position(qapp, target) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([2, 4, 6], [5, 7, 9])
    panel._apply_all_combo.setCurrentIndex(panel._apply_all_combo.findData(target))
    panel._apply_all_btn.click()
    assert panel.selected_pairs() == ([2, 4, 6], [target] * 3)


def test_uniform_re_ce_rejects_out_of_range(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([1, 2], [1, 1])
    panel._apply_uniform_re_ce(17)
    assert panel.selected_pairs() == ([1, 2], [1, 1])


def test_select_all_and_none_keep_the_re_ce(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs(list(range(1, 17)), [ON_BOARD_RE_CE_CHANNEL] * 16)
    panel.select_none()
    assert panel.selected_pairs() == ([], [])
    panel.select_all()
    assert panel.selected_pairs() == (list(range(1, 17)), [16] * 16)


def test_pairs_changed_signal(qapp) -> None:
    panel = ElectrodeWiringPanel()
    captured: list[tuple[list[int], list[int]]] = []
    panel.pairs_changed.connect(
        lambda we, re_ce: captured.append((list(we), list(re_ce)))
    )
    panel.set_pairs([5, 9], [5, 9])
    assert captured[-1] == ([5, 9], [5, 9])
    panel._enable_boxes[0].setChecked(True)
    assert captured[-1] == ([1, 5, 9], [15, 5, 9])


def test_set_pairs_overwrites_enable_state(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([1, 2, 3], [1, 2, 3])
    panel.set_pairs([5], [5])
    assert panel.selected_pairs() == ([5], [5])


def test_lengths_always_match_and_order_is_by_channel(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([16, 3, 1], [16, 16, 16])
    we, re_ce = panel.selected_pairs()
    assert we == [1, 3, 16] and re_ce == [16, 16, 16]
    assert panel.selected_channels() == we
