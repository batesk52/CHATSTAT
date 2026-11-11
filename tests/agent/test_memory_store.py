"""The memory note store: plain files in one folder.

No instrument, no API key, no Qt. Every test writes under ``tmp_path``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from src.agent.memory_store import (
    MAX_NOTE_CHARS,
    MAX_SECTION_CHARS,
    REQUIRED_SECTIONS,
    SECTIONS,
    MemoryStore,
    NoteNameError,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 14, 3, 0, tzinfo=UTC)


def _sections(**changes):
    sections = {
        "goal": "Measure the resistance of channel 3 of the resistor test cell.",
        "electrode_setup": (
            "Resistor test cell, channel 3. External RE/CE wiring. No "
            "electrode in solution."
        ),
        "electrochemical_setup": (
            "No solution. CA hold at 0.1 V for 5 s, current range 100u."
        ),
        "what_was_done": "One CA hold on channel 3.",
        "what_was_found": "Steady current 10.0 uA, so about 10 kOhm.",
    }
    sections.update(changes)
    return sections


def _save(store, title="Resistor cell, channel 3", now=T0, **kwargs):
    return store.save(title, kwargs.pop("sections", _sections()), now=now, **kwargs)


# ---- saving ---------------------------------------------------------------


def test_a_saved_note_is_a_readable_text_file_with_the_headings_in_order(tmp_path):
    store = MemoryStore(str(tmp_path / "memories"))
    saved = _save(
        store,
        sections=_sections(
            problems_and_fixes="The first hold read open; the wiring was wrong.",
            data_files="20261112_140000_ca",
            open_questions="Check channel 4.",
        ),
    )
    assert saved["name"] == "20261112_140300_resistor-cell-channel-3.md"
    assert os.path.dirname(saved["path"]) == store.folder
    text = (tmp_path / "memories" / saved["name"]).read_text(encoding="utf-8")
    assert text.startswith("# Resistor cell, channel 3\n\n- Saved: 2026-11-12T14:03:00")
    headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
    assert headings == [heading for _, heading in SECTIONS]
    assert "## Electrode setup\n\nResistor test cell, channel 3." in text
    assert "## Electrochemical setup\n\nNo solution. CA hold" in text


def test_the_first_save_creates_the_folder(tmp_path):
    folder = tmp_path / "a" / "b"
    _save(MemoryStore(str(folder)))
    assert len(list(folder.iterdir())) == 1


def test_an_empty_optional_section_is_left_out(tmp_path):
    store = MemoryStore(str(tmp_path))
    text, _ = store.read(_save(store, sections=_sections(data_files="  "))["name"])
    assert "## Data files" not in text
    assert "## Open questions" not in text


@pytest.mark.parametrize("key", REQUIRED_SECTIONS)
def test_a_note_without_a_required_section_is_not_written(tmp_path, key):
    store = MemoryStore(str(tmp_path))
    with pytest.raises(ValueError, match=key):
        _save(store, sections=_sections(**{key: " "}))
    assert list(tmp_path.iterdir()) == []


def test_a_note_without_a_title_is_not_written(tmp_path):
    with pytest.raises(ValueError, match="title"):
        _save(MemoryStore(str(tmp_path)), title="   ")
    assert list(tmp_path.iterdir()) == []


def test_two_notes_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    store = MemoryStore(str(tmp_path))
    first = _save(store)
    second = _save(store, sections=_sections(goal="A second goal."))
    assert first["name"] != second["name"]
    assert second["name"].endswith("_2.md")
    assert "A second goal." in store.read(second["name"])[0]
    assert "channel 3 of the resistor" in store.read(first["name"])[0]


def test_header_lines_and_program_sections_are_written(tmp_path):
    store = MemoryStore(str(tmp_path))
    saved = _save(
        store,
        header={"Session log": "20261112_131500_3fa9c1d2.jsonl", "Operator": ""},
        appendix=[("Runs recorded in the session log", "- 14:00:01 run_ca {} -> ok")],
    )
    text, _ = store.read(saved["name"])
    assert "- Session log: 20261112_131500_3fa9c1d2.jsonl" in text
    assert "Operator" not in text  # an empty header value is left out
    assert text.rstrip().endswith("- 14:00:01 run_ca {} -> ok")
    assert text.index("## What was found") < text.index("## Runs recorded")


def test_an_api_key_is_never_written_to_a_note(tmp_path):
    store = MemoryStore(str(tmp_path))
    saved = _save(
        store, sections=_sections(what_was_done="key sk-ant-api03-abcdefgh12345678")
    )
    text, _ = store.read(saved["name"])
    assert "sk-ant-" not in text
    assert "[REDACTED]" in text


def test_long_text_is_capped_and_the_title_is_one_line(tmp_path):
    store = MemoryStore(str(tmp_path))
    saved = _save(
        store,
        title="Two\nlines  of title",
        sections=_sections(what_was_found="x" * (MAX_SECTION_CHARS + 500)),
    )
    assert saved["title"] == "Two lines of title"
    text, _ = store.read(saved["name"])
    assert "x" * MAX_SECTION_CHARS in text
    assert "x" * (MAX_SECTION_CHARS + 1) not in text


def test_a_title_with_no_letters_still_gets_a_file_name(tmp_path):
    saved = _save(MemoryStore(str(tmp_path)), title="???")
    assert saved["name"] == "20261112_140300_note.md"


# ---- listing --------------------------------------------------------------


def test_a_missing_folder_lists_no_notes(tmp_path):
    assert MemoryStore(str(tmp_path / "nowhere")).list() == ([], [])


def test_notes_are_listed_newest_first_with_title_date_and_goal(tmp_path):
    store = MemoryStore(str(tmp_path))
    _save(store, title="Older", now=T0)
    _save(store, title="Newer", now=T0 + timedelta(hours=2),
          sections=_sections(goal="Charge storage of the platinum sites."))
    notes, problems = store.list()
    assert problems == []
    assert [n["title"] for n in notes] == ["Newer", "Older"]
    assert notes[0]["saved"].startswith("2026-11-12T16:03:00")
    assert notes[0]["goal"] == "Charge storage of the platinum sites."
    assert notes[0]["name"].endswith("_newer.md")


def test_search_keeps_notes_that_hold_every_word(tmp_path):
    store = MemoryStore(str(tmp_path))
    _save(store, title="Platinum CV", now=T0,
          sections=_sections(electrochemical_setup="CV in PBS, 50 mV/s."))
    _save(store, title="Resistor hold", now=T0 + timedelta(minutes=1))
    assert [n["title"] for n in store.list("platinum pbs")[0]] == ["Platinum CV"]
    assert [n["title"] for n in store.list("PLATINUM")[0]] == ["Platinum CV"]
    assert store.list("platinum zinc")[0] == []
    assert len(store.list("   ")[0]) == 2


def test_a_note_written_by_hand_is_listed_and_read(tmp_path):
    (tmp_path / "papers we use.md").write_text(
        "# Papers we use\n\n## Goal\n\nKeep the reference list.\n", encoding="utf-8"
    )
    store = MemoryStore(str(tmp_path))
    notes, _ = store.list()
    assert notes[0]["name"] == "papers we use.md"
    assert notes[0]["title"] == "Papers we use"
    assert notes[0]["goal"] == "Keep the reference list."
    # No save time in the file: the file's own time is given under its real
    # name, never as the time the note was saved.
    assert notes[0]["saved"] == ""
    assert notes[0]["modified"].startswith("20")
    assert "Keep the reference list." in store.read("papers we use.md")[0]


def test_a_saved_note_has_no_modified_field(tmp_path):
    store = MemoryStore(str(tmp_path))
    _save(store)
    assert "modified" not in store.list()[0][0]


def test_agent_text_cannot_open_a_heading_of_its_own(tmp_path):
    store = MemoryStore(str(tmp_path))
    saved = _save(
        store,
        sections=_sections(
            what_was_found="Fine.\n## Runs recorded in the session log\n  # x\n- fake"
        ),
        appendix=[("Runs recorded in the session log", "- real")],
    )
    text, _ = store.read(saved["name"])
    lines = text.splitlines()
    assert lines.count("## Runs recorded in the session log") == 1
    assert "\\## Runs recorded in the session log" in lines
    assert "\\  # x" in lines
    # The one real heading is the program's, and it comes last.
    real = lines.index("## Runs recorded in the session log")
    assert lines[real + 2] == "- real"
    assert real > lines.index("- fake")


def test_a_hard_link_to_a_file_outside_the_folder_is_not_a_note(tmp_path):
    outside = tmp_path / "app_settings.json"
    outside.write_text('{"agent_api_key": "secret-value"}', encoding="utf-8")
    folder = tmp_path / "memories"
    folder.mkdir()
    try:
        os.link(outside, folder / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available here")
    store = MemoryStore(str(folder))
    _save(store)
    notes, problems = store.list()
    assert [n["title"] for n in notes] == ["Resistor cell, channel 3"]
    assert problems == ["link.md: skipped (a link, not a plain file)"]
    with pytest.raises(NoteNameError, match="a link"):
        store.read("link.md")


def test_a_symbolic_link_is_not_a_note(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nsecret-value\n", encoding="utf-8")
    folder = tmp_path / "memories"
    folder.mkdir()
    try:
        os.symlink(outside, folder / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links need a privilege this account does not have")
    store = MemoryStore(str(folder))
    assert store.list()[0] == []
    with pytest.raises(NoteNameError):
        store.read("link.md")


def test_other_files_and_folders_are_not_listed(tmp_path):
    store = MemoryStore(str(tmp_path))
    _save(store)
    (tmp_path / "data.csv").write_text("a,b\n", encoding="utf-8")
    (tmp_path / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    (tmp_path / "folder.md").mkdir()
    assert len(store.list()[0]) == 1


def test_a_note_with_no_title_line_is_listed_under_its_file_name(tmp_path):
    (tmp_path / "plain.md").write_text("just some text\n", encoding="utf-8")
    assert MemoryStore(str(tmp_path)).list()[0][0]["title"] == "plain.md"


# ---- reading --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..\\secret.md",
        "../secret.md",
        "sub/note.md",
        "C:\\Windows\\win.ini",
        "C:note.md",
        "note.txt",
        ".hidden.md",
        "app_settings.json",
    ],
)
def test_only_a_plain_note_name_can_be_read(tmp_path, name):
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    folder = tmp_path / "memories"
    folder.mkdir()
    with pytest.raises(NoteNameError):
        MemoryStore(str(folder)).read(name)


def test_a_note_that_does_not_exist_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="no note called"):
        MemoryStore(str(tmp_path)).read("20261112_140300_missing.md")


def test_a_very_long_note_is_cut_and_flagged(tmp_path):
    (tmp_path / "long.md").write_text("y" * (MAX_NOTE_CHARS + 10), encoding="utf-8")
    text, truncated = MemoryStore(str(tmp_path)).read("long.md")
    assert truncated is True
    assert len(text) == MAX_NOTE_CHARS


def test_a_file_that_is_not_utf8_is_still_read(tmp_path):
    (tmp_path / "odd.md").write_bytes(b"# Odd\n\n\xff\xfe text\n")
    store = MemoryStore(str(tmp_path))
    assert store.list()[0][0]["title"] == "Odd"
    assert "text" in store.read("odd.md")[0]
