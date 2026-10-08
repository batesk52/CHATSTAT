"""Strict preset-import behavior (PR #13 review finding #3).

``PresetManager.load_from_path`` must parse strictly and raise BEFORE
touching any manager state: a corrupt or wrong file previously emptied
the in-memory presets, repointed the active store at the bad file (so
the next save overwrote it), and surfaced nothing. These tests pin the
fixed contract: failure -> exception + manager untouched; success ->
presets merged over built-ins + store repointed.

Also covers the shipped built-in defaults (a fresh machine with no
migrated store must not present an empty preset list).
"""

from __future__ import annotations

import json

import pytest

from src.data.presets import Preset, PresetManager


def _store_with_preset(tmp_path, key="mine"):
    """Return a manager on a temp store holding one user preset."""
    mgr = PresetManager(path=str(tmp_path / "store.mux16"))
    mgr.add_preset(
        key, Preset(name=key, technique="cv", channels=[1, 2])
    )
    return mgr


def test_corrupt_file_raises_and_leaves_manager_untouched(tmp_path):
    """Non-JSON content raises ValueError; presets and path unchanged."""
    mgr = _store_with_preset(tmp_path)
    before_path = mgr.path
    before_keys = mgr.list_presets()

    bad = tmp_path / "corrupt.mux16"
    bad.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ValueError):
        mgr.load_from_path(str(bad))

    assert mgr.path == before_path  # store NOT repointed
    assert mgr.list_presets() == before_keys  # presets NOT wiped
    assert mgr.get_preset("mine") is not None


def test_wrong_shape_raises_and_leaves_manager_untouched(tmp_path):
    """Valid JSON that isn't a preset store raises ValueError."""
    mgr = _store_with_preset(tmp_path)
    before_keys = mgr.list_presets()

    wrong = tmp_path / "wrong.mux16"
    wrong.write_text(
        json.dumps({"presets": {"x": "not-a-preset-dict"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        mgr.load_from_path(str(wrong))
    assert mgr.list_presets() == before_keys


def test_missing_file_raises_oserror(tmp_path):
    """A nonexistent path raises OSError; manager untouched."""
    mgr = _store_with_preset(tmp_path)
    with pytest.raises(OSError):
        mgr.load_from_path(str(tmp_path / "nope.mux16"))
    assert mgr.get_preset("mine") is not None


def test_valid_file_loads_and_repoints(tmp_path):
    """A valid store loads, merges over built-ins, and repoints."""
    src = PresetManager(path=str(tmp_path / "src.mux16"))
    src.add_preset(
        "imported", Preset(name="imported", technique="ca", channels=[3])
    )

    mgr = _store_with_preset(tmp_path)
    mgr.load_from_path(str(tmp_path / "src.mux16"))

    assert mgr.get_preset("imported") is not None
    assert mgr.path == str(tmp_path / "src.mux16")
    # The previous store's user preset is replaced by the new file's
    # contents (built-ins re-seeded underneath).
    assert mgr.get_preset("mine") is None


def test_builtin_defaults_always_present(tmp_path):
    """A brand-new store ships generic CV/EIS/CA built-ins.

    Guards the fresh-clone / packaged-exe case where no migrated user
    store exists: the dropdown and the sequencer's "Add step" must not
    be empty.
    """
    mgr = PresetManager(path=str(tmp_path / "fresh.mux16"))
    for key, technique in (
        ("default_cv", "cv"),
        ("default_eis", "eis"),
        ("default_ca", "ca"),
    ):
        preset = mgr.get_preset(key)
        assert preset is not None
        assert preset.technique == technique
        assert preset.channels == [1]
        assert mgr.is_builtin(key)  # protected from deletion
