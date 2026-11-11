"""Memory notes: plain text files that carry context from one chat to the next.

A note records what the raw data does not: what a run was for, how the
electrodes and the cell were set up, what was done, what was found, and what
went wrong. One note is one Markdown file in the memory folder, so a person
can read, edit or delete it with any text editor, and a note written by hand
is found the same way as one the agent saved.

A saved note looks like this::

    # Charge storage of sputtered platinum sites

    - Saved: 2026-11-12T14:03:00-05:00
    - Session log: 20261112_131500_3fa9c1d2.jsonl
    - Model: claude-sonnet-5-5

    ## Goal
    ...
    ## Electrode setup
    ...
    ## Electrochemical setup
    ...

The headings, in order, are :data:`SECTIONS`. A note is context, not data:
it is the saving session's summary and nothing checks it against the
measurements. The exported files it names are the record.

This module has no Qt and no agent in it; ``src.agent.memory_tools`` puts
the agent's three tools on top of it.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_NOTE_CHARS",
    "MAX_SECTION_CHARS",
    "MAX_TITLE_CHARS",
    "NOTE_SUFFIX",
    "REQUIRED_SECTIONS",
    "SECTIONS",
    "MemoryStore",
    "NoteNameError",
]

#: ``(key, heading)`` of each section of a note, in file order.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("goal", "Goal"),
    ("electrode_setup", "Electrode setup"),
    ("electrochemical_setup", "Electrochemical setup"),
    ("what_was_done", "What was done"),
    ("what_was_found", "What was found"),
    ("problems_and_fixes", "Problems and fixes"),
    ("data_files", "Data files"),
    ("open_questions", "Open questions"),
)

#: Sections a saved note must have.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "goal",
    "electrode_setup",
    "electrochemical_setup",
    "what_was_done",
    "what_was_found",
)

NOTE_SUFFIX = ".md"
MAX_TITLE_CHARS = 120
MAX_SECTION_CHARS = 6000
#: Longest note text handed back by :meth:`MemoryStore.read`.
MAX_NOTE_CHARS = 60000
_MAX_SLUG_CHARS = 40
_MAX_GOAL_PREVIEW = 200
_BAD_NAME_CHARS = '/\\:*?"<>|'

#: Anything shaped like an Anthropic API key (never written to a note).
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_REDACTED = "[REDACTED]"


class NoteNameError(ValueError):
    """A note name that is not a plain ``.md`` file name in the folder."""


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _body(value: Any, limit: int = MAX_SECTION_CHARS) -> str:
    """Section text: line breaks kept, ends trimmed, length capped."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()[:limit]


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:_MAX_SLUG_CHARS].strip("-") or "note"


def _section_text(text: str, heading: str) -> str:
    """The text under ``## heading``, up to the next ``## `` line."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


class MemoryStore:
    """The notes in one folder.

    Args:
        folder: The memory folder. It need not exist: listing a missing
            folder gives no notes, and the first save creates it.
    """

    def __init__(self, folder: str) -> None:
        self._folder = os.path.abspath(os.path.expanduser(folder))

    @property
    def folder(self) -> str:
        """The memory folder (absolute)."""
        return self._folder

    # ---- writing ---------------------------------------------------------

    def save(
        self,
        title: str,
        sections: Mapping[str, Any],
        *,
        header: Optional[Mapping[str, Any]] = None,
        appendix: Sequence[tuple[str, str]] = (),
        now: Optional[datetime] = None,
    ) -> dict[str, str]:
        """Write one note and return its ``name``, ``path``, ``title``, ``saved``.

        Args:
            title: The note's title (one line).
            sections: Text by section key (see :data:`SECTIONS`). Unknown
                keys are ignored; empty sections are left out.
            header: Extra ``- Label: value`` lines under the title, for
                example the session log's file name.
            appendix: ``(heading, text)`` sections written after the named
                ones, for text the program (not the agent) supplies.
            now: The save time (for tests); the local time by default.

        Raises:
            ValueError: With no title or with a required section missing.
            OSError: If the folder or the file cannot be written.
        """
        title = _one_line(title, MAX_TITLE_CHARS)
        if not title:
            raise ValueError("A note needs a title.")
        bodies = {key: _body(sections.get(key)) for key, _ in SECTIONS}
        missing = [key for key in REQUIRED_SECTIONS if not bodies[key]]
        if missing:
            raise ValueError(
                "A note needs these sections: " + ", ".join(missing) + "."
            )

        moment = now if now is not None else datetime.now().astimezone()
        saved = moment.isoformat(timespec="seconds")
        lines = [f"# {title}", "", f"- Saved: {saved}"]
        for label, value in (header or {}).items():
            value = _one_line(value, 300)
            if value:
                lines.append(f"- {_one_line(label, 60)}: {value}")
        for key, heading in SECTIONS:
            if bodies[key]:
                lines += ["", f"## {heading}", "", bodies[key]]
        for heading, text in appendix:
            text = _body(text, MAX_SECTION_CHARS * 2)
            if text:
                lines += ["", f"## {_one_line(heading, 80)}", "", text]
        content = _KEY_RE.sub(_REDACTED, "\n".join(lines) + "\n")

        os.makedirs(self._folder, exist_ok=True)
        stem = f"{moment.strftime('%Y%m%d_%H%M%S')}_{_slug(title)}"
        for attempt in range(1, 100):
            name = stem + ("" if attempt == 1 else f"_{attempt}") + NOTE_SUFFIX
            path = os.path.join(self._folder, name)
            try:
                # "x": never write over a note another session just saved.
                with open(path, "x", encoding="utf-8", newline="\n") as fh:
                    fh.write(content)
            except FileExistsError:
                continue
            return {"name": name, "path": path, "title": title, "saved": saved}
        raise OSError(f"Could not find a free file name for {stem!r}.")

    # ---- reading ---------------------------------------------------------

    def list(
        self, search: Optional[str] = None
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Return ``(notes, problems)``, newest note first.

        Each note is ``{"name", "title", "saved", "goal"}``. ``search``
        keeps the notes whose text holds every word of it (any case).
        ``problems`` names each file that could not be read; one bad file
        never hides the others.
        """
        try:
            names = sorted(os.listdir(self._folder))
        except FileNotFoundError:
            return [], []
        except OSError as exc:
            return [], [f"The memory folder could not be read: {exc}"]
        words = [w for w in (search or "").lower().split() if w]
        rows: list[tuple[float, dict[str, str]]] = []
        problems: list[str] = []
        for name in names:
            path = os.path.join(self._folder, name)
            if name.startswith(".") or not name.lower().endswith(NOTE_SUFFIX):
                continue
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(MAX_NOTE_CHARS)
                modified = os.path.getmtime(path)
            except OSError as exc:
                problems.append(f"{name}: could not be read ({exc})")
                continue
            if words and not all(w in (name + "\n" + text).lower() for w in words):
                continue
            rows.append(self._describe(name, text, modified))
        rows.sort(key=lambda row: (row[0], row[1]["name"]), reverse=True)
        return [note for _, note in rows], problems

    @staticmethod
    def _describe(
        name: str, text: str, modified: float
    ) -> tuple[float, dict[str, str]]:
        """``(sort time, list entry)`` for one note's text."""
        title, saved = "", ""
        for line in text.splitlines():
            if line.startswith("# ") and not title:
                title = line[2:].strip()
            elif line.startswith("- Saved:") and not saved:
                saved = line.split(":", 1)[1].strip()
            elif line.startswith("## "):
                break
        when = modified
        if saved:
            try:
                when = datetime.fromisoformat(saved).timestamp()
            except ValueError:
                pass
        else:
            saved = (
                datetime.fromtimestamp(modified)
                .astimezone()
                .isoformat(timespec="seconds")
            )
        goal = _one_line(_section_text(text, "Goal"), _MAX_GOAL_PREVIEW)
        entry = {"name": name, "title": title or name, "saved": saved, "goal": goal}
        return when, entry

    def read(self, name: str) -> tuple[str, bool]:
        """Return ``(text, truncated)`` for the note called *name*.

        Args:
            name: A file name from :meth:`list`, with no folder in it.

        Raises:
            NoteNameError: If *name* is not a plain ``.md`` file name, or
                the file it names is not inside the memory folder.
            FileNotFoundError: If there is no such note.
            OSError: If the file cannot be read.
        """
        name = str(name or "").strip()
        if (
            not name
            or name != os.path.basename(name)
            or any(ch in name for ch in _BAD_NAME_CHARS)
            or name.startswith(".")
            or not name.lower().endswith(NOTE_SUFFIX)
        ):
            raise NoteNameError(
                f"{name!r} is not a note name. Use a name from the list, such "
                "as 20261112_140300_title.md, with no folder in it."
            )
        path = os.path.join(self._folder, name)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"There is no note called {name!r}.")
        # A link in the folder must not lead to a file somewhere else.
        inside = os.path.dirname(os.path.realpath(path))
        if os.path.normcase(inside) != os.path.normcase(
            os.path.realpath(self._folder)
        ):
            raise NoteNameError(f"{name!r} is not a file in the memory folder.")
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_NOTE_CHARS + 1)
        return text[:MAX_NOTE_CHARS], len(text) > MAX_NOTE_CHARS
