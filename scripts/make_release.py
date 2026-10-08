"""Build the ChatStat tree from the private development repository.

ChatStat 0.1.0 was created by this script. It copies an allow-list of files
from the development repository into a new folder, applies the text rewrites
listed below, adds the project files, the user README, the example data,
``.gitignore``, ``.gitattributes`` and ``requirements-lock.txt``, and then
scans every text file in the new tree for patterns that must not be
published. Any hit makes the script exit non-zero and lists each one as
``file:line``.

Run it with the Python environment the tests were run in, because the lock
file is frozen from that environment:

    python scripts/make_release.py [--out DIR] [--force] [--python EXE]

The copy of this script inside ChatStat has its private scrub rules redacted.
It records how the tree was built; it cannot rebuild it on its own.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Pattern, Union

# Name and version of the application. The README, the window title and the
# version in src/__init__.py follow these constants.
APP_NAME = "ChatStat"
VERSION = "0.1.0"

REPO_ROOT = Path(__file__).resolve().parents[1]


class Rewrite(NamedTuple):
    """One text rewrite, applied to tree files whose path matches ``glob``.

    ``old`` is a literal string or a compiled regex. Every rule must match at
    least once (with ``each``, in every file the glob selects), so a rule that
    goes stale when its source file changes stops the build.
    """

    glob: str
    old: Union[str, Pattern[str]]
    new: str
    each: bool = False


# -- allow-list --------------------------------------------------------------

TREES = ("src", "tests")  # copied whole, minus caches and EXCLUDED
FILES = ("LICENSE", "requirements.txt", "scripts/paper_screenshots.py")
DOCS = (
    "docs/enclosure_design.md",
    "docs/agent_demo_eis_autorange.md",
    "docs/multiplexer_limitations_and_lessons.md",
    "docs/pre_enclosure_smoke.md",
    "docs/references/pstrace_method_keys.md",
)
SMOKE_SRC = "claude_test_files"  # smoke_*.py -> examples/smoke/
SMOKE_GLOB = "smoke_*.py"
SMOKE_DEST = "examples/smoke"
SELF = "scripts/make_release.py"
# Project files kept in docs/release/ ({{APP_NAME}} is substituted):
# source file name -> path in the tree.
RELEASE_DOCS = {
    "README.md": "README.md",
    "CLAUDE.md": "CLAUDE.md",
    "architecture.md": "architecture.md",
    "product_requirements.md": "product_requirements.md",
    "action_log.md": "action_log.md",
    "lessons.md": "lessons.md",
    "workflow.md": "workflow.md",
    "examples_data_README.md": "examples/data/README.md",
}
RELEASE_DOCS_DIR = "docs/release"
EXCLUDED = {
    # A one-off re-analysis helper for a single internal dataset. Nothing in
    # the application, the tests or the smoke scripts imports it.
    "src/vendor/electrochem_analysis/analysis/eis_redo_helpers.py",
}
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
DATA_DEST = "examples/data"
# The example data files must stay byte-identical, so the scan reports any
# hit inside them instead of failing the build.
DATA_SUFFIXES = (".csv", ".pssession")
DATA_CHANNELS = 16  # every example run exported all sixteen channels

GITIGNORE = """\
# Python
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/

# Run exports (default folders when running from source)
exports/
agent_exports/

# User data. Presets and sequences live in ~/.emstat_pico_mux16/ or wherever
# you save them; copies dropped into this folder stay out of git.
presets/
*.mux16
*.mux16seq

# Raw serial captures (diagnostics)
bench_captures/

# Agent scratch scripts (local only)
claude_test_files/

# Sample session for examples/smoke/smoke_analysis_tools.py. It is a PalmSens
# file, so it is not redistributed here.
examples/smoke/data/
"""

GITATTRIBUTES = """\
# The example data stay byte-identical to what the application exported:
# no line-ending conversion on commit or checkout.
examples/data/** -text
"""

# -- public rewrites (smoke scripts) -----------------------------------------
# Specific rules come before the generic claude_test_files/ rule, because
# several of them quote that path.

_SMOKE = f"{SMOKE_DEST}/*.py"
_ANALYSIS = f"{SMOKE_DEST}/smoke_analysis_tools.py"
_SAMPLE_BLOCK = '''\
# The sample is the demo session that PSTrace installs with its example data
# (Documents/PSData/Demo CV DPV EIS IS-C electrode.pssession). It is a
# PalmSens file, so this repository does not include it: copy it to
# examples/smoke/data/demo_cv_dpv_eis.pssession, or let the second path below
# find PSTrace's own copy. Without it the script prints SMOKE SKIP.
_SAMPLE_PATHS = (
    os.path.join(
        _REPO_ROOT, "examples", "smoke", "data", "demo_cv_dpv_eis.pssession"
    ),
    os.path.join(
        os.path.expanduser("~"), "Documents", "PSData",
        "Demo CV DPV EIS IS-C electrode.pssession",
    ),
)
SAMPLE = next(
    (path for path in _SAMPLE_PATHS if os.path.isfile(path)), _SAMPLE_PATHS[0]
)
'''

PUBLIC_REWRITES: list[Rewrite] = [
    # The smoke scripts sit one folder deeper in the tree.
    Rewrite(
        _SMOKE,
        "_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
        "_REPO_ROOT = os.path.dirname(\n"
        "    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        ")",
        each=True,
    ),
    # The analysis smoke reads a PalmSens demo file that is not shipped.
    Rewrite(
        _ANALYSIS,
        "the bundled sample session claude_test_files/data/demo_cv_dpv_eis.\n"
        "pssession via the REAL src.agent.tools.dispatch_tool path.",
        "the demo session that PSTrace installs (see SAMPLE below) via the\n"
        "REAL src.agent.tools.dispatch_tool path.",
    ),
    Rewrite(
        _ANALYSIS,
        'Prints\n"SMOKE PASS" and exits 0 on success.',
        'Prints\n"SMOKE PASS" and exits 0 on success, or "SMOKE SKIP" and exits 0\n'
        "when the sample session is missing.",
    ),
    Rewrite(
        _ANALYSIS,
        "SAMPLE = os.path.join(\n"
        '    _REPO_ROOT, "claude_test_files", "data", "demo_cv_dpv_eis.pssession"\n'
        ")\n",
        _SAMPLE_BLOCK,
    ),
    Rewrite(
        _ANALYSIS,
        '    """Entry point. Returns the process exit code."""\n'
        "    failures: list[str] = []\n",
        '    """Entry point. Returns the process exit code."""\n'
        "    if not os.path.isfile(SAMPLE):\n"
        '        print("SMOKE SKIP: sample session not found:", SAMPLE)\n'
        "        return 0\n"
        "    failures: list[str] = []\n",
    ),
    # Development-history notes that point at files outside the tree.
    Rewrite(
        f"{SMOKE_DEST}/smoke_engine_adapter.py",
        "re-polling -- see the\n"
        "    # 2026-06-09 Batch 2 entry in action_log.md).",
        "re-polling -- the\n    # runner waits for the engine to go idle).",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_swv_chain.py",
        "the pieces Req 15 actually promises:",
        "the pieces the SWV tools promise:",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_swv_chain.py",
        "forever (the PR #26 pattern).",
        "forever.",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_tools.py",
        "Same fix PR #26 applied to smoke_engine_adapter.py on 2026-07-29; this\n"
        "    gate has the identical race and never inherited it.  See the call site.",
        "smoke_engine_adapter.py has the same race and the same fix.\n"
        "    See the call site.",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_tools.py",
        "(the PR #26 fix, which smoke_engine_adapter.py got and this gate did not).",
        "(the same fix as in smoke_engine_adapter.py).",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_tools.py",
        "the Batch 3 register() seam",
        "the register() seam",
    ),
    Rewrite(
        f"{SMOKE_DEST}/smoke_tools.py",
        "via the Batch 3 seam",
        "via the register() seam",
    ),
    Rewrite(_SMOKE, re.compile(r"Batch \w+ validation gate:"), "Smoke test:"),
    Rewrite(_SMOKE, "(blueprint constraint; avoids", "(avoids"),
    Rewrite(_SMOKE, "claude_test_files/", f"{SMOKE_DEST}/", each=True),
    # The About dialog matched an older technique list.
    Rewrite(
        "src/gui/main_window.py",
        '"Supports 18 electrochemical techniques with real-time "',
        '"Runs five verified electrochemical techniques with real-time "',
    ),
]

# --- BEGIN PRIVATE SCRUB RULES ---
# Redacted from this copy: the rules name internal projects, people,
# sample IDs and local paths, so they stay in the development repository.
# With these lists empty, main() refuses to build.
OUT_FOLDER = APP_NAME
DATA_SOURCES: list[tuple[str, str]] = []
FORBIDDEN_PATTERNS: list[str] = []
WARN_PATTERNS: list[str] = []
PRIVATE_REWRITES: list[Rewrite] = []
# --- END PRIVATE SCRUB RULES ---

DEFAULT_OUT = REPO_ROOT.parent / OUT_FOLDER

_BEGIN = "# --- BEGIN PRIVATE SCRUB RULES ---"
_END = "# --- END PRIVATE SCRUB RULES ---"
_PRIVATE_BLOCK = re.compile(
    "^" + re.escape(_BEGIN) + "$.*?^" + re.escape(_END) + "$",
    re.MULTILINE | re.DOTALL,
)
_PRIVATE_STUB = "\n".join([
    _BEGIN,
    "# Redacted from this copy: the rules name internal projects, people,",
    "# sample IDs and local paths, so they stay in the development repository.",
    "# With these lists empty, main() refuses to build.",
    "OUT_FOLDER = APP_NAME",
    "DATA_SOURCES: list[tuple[str, str]] = []",
    "FORBIDDEN_PATTERNS: list[str] = []",
    "WARN_PATTERNS: list[str] = []",
    "PRIVATE_REWRITES: list[Rewrite] = []",
    _END,
])


# -- lock file ---------------------------------------------------------------

# Runs inside the target interpreter: walks the installed distributions from
# the requirements.txt names through their dependencies (markers evaluated,
# extras skipped) and reports what the lock header needs.
_CLOSURE_CODE = r"""
import importlib.metadata as md, json, platform, sys
try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

lines = json.loads(sys.argv[1])
dists = {}
for dist in md.distributions():
    dists.setdefault(canonicalize_name(dist.metadata["Name"]), dist)
roots = [Requirement(line) for line in lines]
problems = []
for req in roots:
    dist = dists.get(canonicalize_name(req.name))
    if dist is None:
        problems.append(f"{req.name} is not installed")
    elif not req.specifier.contains(dist.version, prereleases=True):
        problems.append(f"{req.name} {dist.version} does not satisfy {req}")
seen, stack = set(), [canonicalize_name(req.name) for req in roots]
while stack:
    name = stack.pop()
    if name in seen or name not in dists:
        continue
    seen.add(name)
    for text in dists[name].requires or []:
        req = Requirement(text)
        if req.marker is None or req.marker.evaluate({"extra": ""}):
            stack.append(canonicalize_name(req.name))
floor, floor_by = (3, 0), []
for name in sorted(seen):
    spec = dists[name].metadata.get("Requires-Python") or ""
    for part in spec.split(","):
        part = part.strip()
        if part.startswith(">="):
            bits = tuple(
                int(b) for b in part[2:].strip().split(".")[:2] if b.isdigit()
            )
            if len(bits) == 2 and bits > floor:
                floor, floor_by = bits, [name]
            elif len(bits) == 2 and bits == floor:
                floor_by.append(name)
pytest = dists.get("pytest")
print(json.dumps({
    "closure": sorted(seen),
    "problems": problems,
    "python": platform.python_version(),
    "system": f"{platform.system()} {platform.release()}",
    "python_floor": ".".join(map(str, floor)),
    "python_floor_by": floor_by,
    "pytest": pytest.version if pytest else None,
}))
"""


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pin_name(line: str) -> str:
    return _canonical(re.split(r"[=<>@ ;\[]", line, 1)[0])


def build_lock(python: str) -> tuple[str, dict]:
    """Return the lock file text and the facts used in its header."""
    lines = []
    for raw in (REPO_ROOT / "requirements.txt").read_text("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    info = json.loads(
        subprocess.run(
            [python, "-c", _CLOSURE_CODE, json.dumps(lines)],
            check=True, capture_output=True, text=True,
        ).stdout
    )
    if info["problems"]:
        raise SystemExit(
            "the lock environment does not match requirements.txt:\n  "
            + "\n  ".join(info["problems"])
        )
    freeze = subprocess.run(
        [python, "-m", "pip", "freeze"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    wanted = set(info["closure"])
    pins = sorted((line for line in freeze if _pin_name(line) in wanted),
                  key=_pin_name)
    found = {_pin_name(line) for line in pins}
    if found != wanted:
        raise SystemExit(f"pip freeze is missing: {sorted(wanted - found)}")
    header = [
        "# Exact versions from the environment this tree was built and",
        "# tested in: the packages the application imports and everything",
        "# they depend on, from `pip freeze`, trimmed to that set.",
        f"# Python {info['python']} on {info['system']}.",
        f"# These pins need Python >= {info['python_floor']} (set by "
        f"{', '.join(info['python_floor_by'])}); requirements.txt",
        "# lists the looser minimum versions.",
    ]
    if info["pytest"]:
        header.append(
            f"# The test suite also needs pytest (run with pytest {info['pytest']})."
        )
    return "\n".join(header + pins) + "\n", info


# -- build steps -------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_out(out: Path, force: bool) -> None:
    """Create ``out``, or clear an earlier tree there (keeping .git)."""
    if out == REPO_ROOT or REPO_ROOT in out.parents or out in REPO_ROOT.parents:
        raise SystemExit(f"refusing: {out} overlaps the development repository")
    if out.exists():
        if not force:
            raise SystemExit(f"{out} exists; pass --force to rebuild it")
        entries = [p for p in out.iterdir() if p.name != ".git"]
        if entries and not (out / SELF).is_file():
            raise SystemExit(
                f"refusing to clear {out}: it does not look like a built tree"
            )
        for path in entries:
            is_junction = getattr(os.path, "isjunction", lambda _p: False)(path)
            if path.is_symlink() or is_junction:
                # Remove the link itself, never what it points to.
                if path.is_dir():
                    os.rmdir(path)
                else:
                    path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    out.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")


def copy_allow_list(out: Path) -> list[str]:
    """Copy the allow-list and the project files; return their tree paths."""
    copied: list[str] = []

    def put(src: Path, rel: str) -> None:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        copied.append(rel)

    for tree in TREES:
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT / tree):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                src = Path(dirpath) / name
                rel = src.relative_to(REPO_ROOT).as_posix()
                if src.suffix in SKIP_SUFFIXES or rel in EXCLUDED:
                    continue
                put(src, rel)
    for rel in FILES + DOCS:
        put(REPO_ROOT / rel, rel)
    smoke = sorted((REPO_ROOT / SMOKE_SRC).glob(SMOKE_GLOB))
    if not smoke:
        raise SystemExit(f"no {SMOKE_GLOB} files in {SMOKE_SRC}/")
    for src in smoke:
        put(src, f"{SMOKE_DEST}/{src.name}")

    self_text = (REPO_ROOT / SELF).read_text("utf-8")
    redacted, count = _PRIVATE_BLOCK.subn(lambda _m: _PRIVATE_STUB, self_text)
    if count != 1:
        raise SystemExit(f"{SELF}: expected one private block, found {count}")
    _write_text(out / SELF, redacted)
    copied.append(SELF)

    for source, rel in RELEASE_DOCS.items():
        text = (REPO_ROOT / RELEASE_DOCS_DIR / source).read_text("utf-8")
        text = text.replace("{{APP_NAME}}", APP_NAME)
        if "{{" in text:
            raise SystemExit(f"{RELEASE_DOCS_DIR}/{source}: unknown {{{{...}}}} token")
        _write_text(out / rel, text)
        copied.append(rel)
    if APP_NAME not in (out / "README.md").read_text("utf-8"):
        raise SystemExit("README.md does not name the application")
    _write_text(out / ".gitignore", GITIGNORE)
    _write_text(out / ".gitattributes", GITATTRIBUTES)
    copied += [".gitignore", ".gitattributes"]

    init = out / "src" / "__init__.py"
    init_text = init.read_text("utf-8")
    if "__version__" not in init_text:
        _write_text(init, init_text + f'__version__ = "{VERSION}"\n')
    return copied


def copy_data(out: Path) -> list[str]:
    """Copy the example datasets byte for byte; return their tree paths."""
    copied: list[str] = []
    for source, rel_dir in DATA_SOURCES:
        files = sorted(p for p in Path(source).iterdir() if p.is_file())
        csvs = [p for p in files if p.suffix == ".csv"]
        sessions = [p for p in files if p.suffix == ".pssession"]
        others = [p.name for p in files if p.suffix not in DATA_SUFFIXES]
        if others or len(csvs) != DATA_CHANNELS or len(sessions) != 1:
            raise SystemExit(
                f"{rel_dir}: expected {DATA_CHANNELS} CSV files and one "
                f".pssession, found {[p.name for p in files]}"
            )
        for src in csvs + sessions:
            dest = out / rel_dir / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            if _sha256(src) != _sha256(dest):
                raise SystemExit(f"{rel_dir}/{src.name}: copy is not byte-identical")
            copied.append(f"{rel_dir}/{src.name}")
    return copied


def apply_rewrites(out: Path, files: list[str], rules: list[Rewrite]) -> list[str]:
    """Apply ``rules`` in order; return one log line per (rule, file) match."""
    log: list[str] = []
    errors: list[str] = []
    for rule in rules:
        targets = [f for f in files if fnmatch.fnmatchcase(f, rule.glob)]
        total = 0
        for rel in targets:
            path = out / rel
            raw = path.read_bytes().decode("utf-8")  # keep CRLF visible
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n") if crlf else raw
            if isinstance(rule.old, str):
                count = text.count(rule.old)
                text = text.replace(rule.old, rule.new)
                shown = rule.old
            else:
                text, count = rule.old.subn(rule.new, text)
                shown = f"re:{rule.old.pattern}"
            if count:
                path.write_text(
                    text.replace("\n", "\r\n") if crlf else text,
                    "utf-8", newline="",
                )
                log.append(f"{rel} | {count}x | {shown!r} -> {rule.new!r}")
            elif rule.each:
                errors.append(f"{rel}: no match for {shown!r}")
            total += count
        if total == 0:
            errors.append(f"{rule.glob}: rule matched nothing: {rule.old!r}")
    if errors:
        raise SystemExit("stale rewrite rules:\n  " + "\n  ".join(errors))
    return log


def check_version(out: Path) -> None:
    """The About dialog's version must match src.__version__."""
    window = (out / "src" / "gui" / "main_window.py").read_text("utf-8")
    if f'APP_VERSION = "{VERSION}"' not in window:
        raise SystemExit(f"src/gui/main_window.py APP_VERSION is not {VERSION}")
    if f'__version__ = "{VERSION}"' not in (out / "src" / "__init__.py").read_text(
        "utf-8"
    ):
        raise SystemExit(f"src/__init__.py __version__ is not {VERSION}")


def _read_text(path: Path) -> str | None:
    """Return the file's text, or None for a binary file."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def scan(out: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (hits, warnings, data-file hits, unscanned binaries)."""
    forbidden = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
    warn = [re.compile(p, re.IGNORECASE) for p in WARN_PATTERNS]
    hits: list[str] = []
    warnings: list[str] = []
    data_hits: list[str] = []
    binaries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(out):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(out).as_posix()
            is_data = rel.startswith(f"{DATA_DEST}/") and rel.endswith(DATA_SUFFIXES)
            text = _read_text(path)
            if text is None:
                binaries.append(rel)
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for rx in forbidden:
                    for m in rx.finditer(line):
                        entry = f"{rel}:{number}: {m.group(0)!r} [{rx.pattern}]"
                        (data_hits if is_data else hits).append(entry)
                for rx in warn:
                    for m in rx.finditer(line):
                        warnings.append(f"{rel}:{number}: {m.group(0)!r}")
    return hits, warnings, data_hits, binaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None,
                        help=f"output folder (default: {DEFAULT_OUT})")
    parser.add_argument("--force", action="store_true",
                        help="rebuild an existing output folder (its .git is kept)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter whose environment is frozen into "
                             "requirements-lock.txt (default: this one)")
    args = parser.parse_args(argv)

    if not FORBIDDEN_PATTERNS:
        print("This copy has its scrub rules redacted, so it cannot build "
              "the tree.", file=sys.stderr)
        return 2
    out = (args.out or DEFAULT_OUT).resolve()
    lock_text, lock_info = build_lock(args.python)  # fail before touching out
    prepare_out(out, args.force)
    files = copy_allow_list(out)
    _write_text(out / "requirements-lock.txt", lock_text)
    files.append("requirements-lock.txt")
    data = copy_data(out)
    log = apply_rewrites(out, files, PUBLIC_REWRITES + PRIVATE_REWRITES)
    check_version(out)

    print(f"{APP_NAME} {VERSION} tree: {out}")
    print(f"  {len(files) + len(data)} files ({len(data)} example data files, "
          "byte-identical by SHA-256)")
    print(f"  lock: {len(lock_info['closure'])} packages, Python "
          f"{lock_info['python']} ({lock_info['system']})")
    print(f"  rewrites ({len(log)}):")
    for line in log:
        print("    " + line)
    hits, warnings, data_hits, binaries = scan(out)
    for line in warnings:
        print("  WARN " + line)
    for line in data_hits:
        print("  DATA " + line)
    for rel in binaries:
        print("  binary, not scanned: " + rel)
    if hits:
        for line in hits:
            print("  HIT  " + line)
        print(f"SCAN FAILED: {len(hits)} hit(s)")
        return 1
    print(f"SCAN PASSED: 0 hits, {len(warnings)} warning(s), "
          f"{len(data_hits)} hit(s) inside data files (reported, not failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
