"""`diffdata clean-history` (#150, item 7): clear this repo's Claude Code session history.

Collection runs through Claude's Slack connector, so Claude reads whole channels, including
messages from people who opted out, and those reads sit in Claude Code's session history on
this computer. The sign-up procedure asks the producer to clear it after collecting. This
command does what was a manual `rm` of ~/.claude/projects/*differential-data-collection*, on
macOS, Linux and Windows, without the glob's risk of hitting another project.

Claude Code keeps each folder's sessions in <its folder>/projects/<the folder's path, with
every character other than a letter or digit made "-">: ~/.claude, or CLAUDE_CONFIG_DIR if set.
Because "/" and "-" both become "-", a name alone can't tell this repo from a sibling such as
differential-data-collection-old. So a project folder is cleared only if its name is this
repo's (or a folder inside it, such as a worktree) AND every session file in it records a
working folder (`cwd`) inside this repo.

What it refuses (#150's must-refuse list, approved on 2026-11-26):
  4  to run unless its input and output are a terminal a person types in, or when CLAUDECODE
     is set (Claude Code sets it in the shells it runs). clean() checks this itself, so no
     route to the deletion skips it. .claude/settings.json also denies it to Claude's shell
     tools
  5  to delete anything before showing each folder it will delete, with its number of
     sessions, and getting the typed word yes (y, Enter or anything else asks again; no stops)
  6  to touch another project's sessions: a folder whose name matches but which holds a
     transcript (any *.jsonl* file, at any depth) with no `cwd`, or a `cwd` outside this repo,
     or no session file, is left alone and named. A `cwd` that exists is compared by file
     identity, so a link inside the repo that leads elsewhere doesn't count as inside
  7  to touch anything outside the matched project folders: never Claude's folder itself, its
     settings, history.jsonl, other projects, the data folder or ~/.diffdata. It stops if
     Claude's folder or projects/ is a link or overlaps the data folder or ~/.diffdata, or if
     config.toml is there but doesn't load (the data folder isn't known). A matched folder that
     is a link, resolves outside projects/, overlaps a protected folder, or holds anywhere
     inside a link, a junction, a mount point, a file with other hard links, or a folder it
     can't read, is left alone (Linux bind mounts too, from /proc/self/mountinfo). Just
     before deleting, each folder must still be the same folder with the same transcripts
     (identity, size and time of each) as on the list; it's then renamed to a name nobody can
     guess and checked once more, or put back and left alone. Names are shown with control
     characters as "?"
  8  to delete anything when nothing matches: it says so, and exits 0

`diffdata push` ends by offering it (cli.py), only after a push that sent every passed copy,
only in a terminal a person types in, and only without CLAUDECODE (must-refuse 9 and 10).

The deletion uses shutil.rmtree, which on macOS and Linux works through open folder handles
and doesn't follow links. A matched folder also goes with whatever Claude Code keeps in it,
such as its memory/ notes for this repo.
"""

import json
import os
import re
import secrets
import shutil
import sys
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath

CLAUDE_MARK = "CLAUDECODE"
REPO = Path(__file__).resolve().parents[3]  # src/diffdata/common/history.py -> the repo
CWD_LINES = 200  # a session file's first lines hold its cwd; never read a whole transcript
NOT_A_TERMINAL = (
    "run `uv run diffdata clean-history` yourself, in your own terminal, with Claude Code "
    "closed. It deletes this repo's Claude Code session history, so only a person typing may "
    "answer, and never Claude."
)
NOTHING_DELETED = "Nothing was deleted."
ASK = "Type yes to delete these folders, or no to keep them: "
OFFER = (
    "Clear this repo's Claude Code session history now? It holds what Claude read from Slack, "
    "including messages from people who opted out. Close Claude Code first."
)
OFFER_ASK = "Type yes to see what it would delete, or press Enter to skip: "
LATER = (
    "When you've finished collecting, clear this repo's Claude Code session history: close "
    "Claude Code, then run `uv run diffdata clean-history` in your own terminal."
)


class HistoryError(Exception):
    """clean-history can't go on. The message says what to do."""


def run_by_claude() -> bool:
    """Whether a Claude Code shell runs this: CLAUDECODE is set, to anything (must-refuse 4).
    An extra layer, not proof."""
    return CLAUDE_MARK in os.environ


def is_terminal(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def by_a_person() -> bool:
    """A terminal a person types in, and not a Claude Code shell. clean() checks this itself,
    so no route to the deletion skips it (must-refuse 4)."""
    return is_terminal(sys.stdin) and is_terminal(sys.stdout) and not run_by_claude()


def claude_folder() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def encode(path: PurePath | str) -> str:
    """A folder's path as Claude Code names its project folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def shown(text: object) -> str:
    """A name to show: control characters become "?", so a folder's name can't change what the
    list on screen says (must-refuse 5)."""
    return "".join("?" if unicodedata.category(c).startswith("C") else c for c in str(text))


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def lexically_inside(path: str, roots: Iterable[str]) -> bool:
    """`path` is one of `roots` or inside one, compared as paths, never as text prefixes."""
    try:
        candidate = PurePath(os.path.normcase(os.path.normpath(path)))
    except (TypeError, ValueError):
        return False
    if not candidate.is_absolute() or ".." in PurePath(path).parts:
        return False
    for root in roots:
        base = PurePath(os.path.normcase(os.path.normpath(root)))
        if candidate == base or base in candidate.parents:
            return True
    return False


def within(path: Path, folder: Path) -> bool:
    """Whether `path` (which exists) is `folder` or inside it, by file identity: no link,
    letter case or other spelling can fool it."""
    real = path.resolve()
    return any(p.exists() and os.path.samefile(p, folder) for p in (real, *real.parents))


def inside_repo(cwd: str, repo: Path, roots: Iterable[str]) -> bool:
    """Whether a session's recorded folder is the repo or inside it. The deepest part of it
    that still exists must be inside the repo by identity; only the rest (such as a removed
    worktree) is compared by its path."""
    try:
        path = Path(cwd)
        if not path.is_absolute() or ".." in path.parts:
            return False
        existing = path
        while not existing.exists():
            if existing.parent == existing:
                return False
            existing = existing.parent
        if existing == path:
            return within(path, repo)
        return within(existing, repo) and lexically_inside(cwd, roots)
    except (OSError, ValueError):
        return False


def overlap(one: Path, other: Path) -> bool:
    """Whether one folder is the other or inside it, either way, by file identity where they
    exist (case-insensitive volumes, links), and by resolved path otherwise."""
    try:
        if one.exists() and other.exists():
            return within(one, other) or within(other, one)
        a, b = one.resolve(), other.resolve()
    except OSError:
        return True
    return a == b or a in b.parents or b in a.parents


def session_cwd(path: Path) -> str | None:
    """The working folder a transcript records, from its first lines, or None."""
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            for number, line in enumerate(file):
                if number >= CWD_LINES:
                    return None
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
                    return entry["cwd"]
    except OSError:
        return None
    return None


def mount_points() -> list[Path]:
    """Every mount point Linux knows of, bind mounts included. Empty elsewhere."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4:
            # Spaces and other characters are written as octal escapes, such as \040.
            found.append(Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])))
    return found


def unsafe_inside(folder: Path) -> str | None:
    """Why deleting `folder` could reach outside it, or None: a mount point (the folder itself
    included), a link, a junction, a file with other hard links, or a folder it can't read."""
    if os.path.ismount(folder):
        return "it's a mount point"
    real = folder.resolve()
    if any(m == real or real in m.parents for m in mount_points()):
        return "it holds a mount point"
    device = folder.lstat().st_dev
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=errors.append, followlinks=False):
        for name in dirnames:
            entry = Path(dirpath) / name
            if is_link(entry):
                return "it holds a link"
            if os.path.ismount(entry) or entry.lstat().st_dev != device:
                return "it holds a mount point"
        for name in filenames:
            entry = Path(dirpath) / name
            if is_link(entry):
                return "it holds a link"
            try:
                info = entry.lstat()
            except OSError:
                return "it holds a file it can't read"
            if info.st_nlink > 1:
                return "it holds a file with other hard links"
    if errors:
        return "it holds a folder it can't read"
    return None


def transcripts(folder: Path) -> list[Path]:
    """Every transcript in a project folder, at any depth: sessions, subagents' and the
    .jsonl.superseded-* copies. Plain files only."""
    return sorted(p for p in folder.rglob("*.jsonl*") if p.is_file() and not p.is_symlink())


@dataclass
class Found:
    projects: Path
    repo: Path
    roots: tuple[str, ...]
    keep: list[Path]
    matched: dict[Path, tuple] = field(default_factory=dict)  # folder -> what was checked
    left: dict[Path, str] = field(default_factory=dict)  # folder -> why it was left alone


def fingerprint(folder: Path, found_transcripts: list[Path]) -> tuple:
    """The folder's identity and every transcript's: what the list showed, to compare just
    before deleting (must-refuse 7)."""
    info = folder.stat(follow_symlinks=False)
    files = []
    for path in found_transcripts:
        meta = path.stat(follow_symlinks=False)
        files.append((str(path.relative_to(folder)), meta.st_ino, meta.st_size, meta.st_mtime_ns))
    return (info.st_dev, info.st_ino, tuple(files))


def sessions_in(checked: tuple) -> int:
    return sum(1 for name, *_ in checked[2] if "/" not in name and "\\" not in name
               and name.endswith(".jsonl"))  # fmt: skip


def check(folder: Path, found: Found) -> tuple | str:
    """What a matched folder holds (its fingerprint), or why it's left alone."""
    if is_link(folder):
        return "it's a link"
    if not folder.is_dir():
        return "not a folder"
    if folder.resolve().parent != found.projects:
        return "it leads outside Claude's projects folder"
    if any(overlap(folder, other) for other in found.keep):
        return "it overlaps your data folder or ~/.diffdata"
    why = unsafe_inside(folder)
    if why:
        return why
    found_transcripts = transcripts(folder)
    sessions = [p for p in found_transcripts if p.parent == folder and p.suffix == ".jsonl"]
    if not sessions:
        return "no session files to check"
    for transcript in found_transcripts:
        cwd = session_cwd(transcript)
        if cwd is None:
            return "a session with no recorded folder"
        if not inside_repo(cwd, found.repo, found.roots):
            return "a session from another folder"
    return fingerprint(folder, found_transcripts)


def find(repo: Path | None = None, data: Path | None = None, projects: Path | None = None) -> Found:
    """This repo's project folders, checked (must-refuse 6 and 7)."""
    repo = (repo or REPO).absolute()
    projects = projects if projects is not None else claude_folder() / "projects"
    keep = [Path.home() / ".diffdata", *([data] if data is not None else [])]
    for part in (projects.parent, projects):
        if is_link(part):
            raise HistoryError(f"{shown(part)} is a link, so clean-history won't delete under it.")
        for other in keep:
            if overlap(part, other):
                raise HistoryError(
                    f"{shown(part)} overlaps {shown(other)}, so clean-history won't delete there."
                )
    # The path as typed and as resolved: Claude Code records whichever the shell gave it.
    roots = tuple(dict.fromkeys([str(repo), str(repo.resolve())]))
    found = Found(projects.resolve() if projects.is_dir() else projects, repo, roots, keep)
    if not projects.is_dir():
        return found
    names = {os.path.normcase(encode(root)) for root in roots}
    for entry in sorted(projects.iterdir()):
        name = os.path.normcase(entry.name)  # Windows paths ignore letter case
        if not any(name == n or name.startswith(n + "-") for n in names):
            continue
        result = check(entry, found)
        if isinstance(result, tuple):
            found.matched[entry] = result
        else:
            found.left[entry] = result
    return found


def delete(folder: Path, checked: tuple, found: Found) -> str | None:
    """Delete one checked folder, or say why not. It's first renamed to a name nobody can
    guess, inside projects/, and must be the very folder that was checked (must-refuse 7)."""
    if check(folder, found) != checked:
        return "it changed while you read the list"
    hidden = found.projects / f".diffdata-deleting-{secrets.token_hex(8)}"
    os.rename(folder, hidden)
    moved = hidden.stat(follow_symlinks=False)
    if (moved.st_dev, moved.st_ino) != checked[:2] or is_link(hidden):
        os.rename(hidden, folder)
        return "it changed while you read the list"
    shutil.rmtree(hidden)
    return None


def clean(
    ask: Callable[[str], str],
    say: Callable[[str], None] = print,
    repo: Path | None = None,
    data: Path | None = None,
    projects: Path | None = None,
) -> int:
    """Show this repo's project folders, ask for the typed word yes, then delete them. Refused
    unless a person in a terminal runs it, and never under Claude Code (must-refuse 4)."""
    if not by_a_person():
        say(f"diffdata clean-history: {NOT_A_TERMINAL} {NOTHING_DELETED}")
        return 1
    try:
        found = find(repo, data, projects)
    except HistoryError as e:
        say(f"{e} {NOTHING_DELETED}")
        return 1
    for folder, why in found.left.items():
        say(f"Left alone: {shown(folder.name)} ({why}).")
    if not found.matched:
        say(
            f"No Claude Code session history for this repo in {shown(found.projects)}. "
            f"{NOTHING_DELETED}"
        )
        return 0
    say(
        f"Claude Code keeps this repo's session history in {shown(found.projects)}. It will delete:"
    )
    for folder, checked in found.matched.items():
        say(f"  {shown(folder.name)}: {sessions_in(checked)} session(s)")
    say(
        "That includes what Claude Code keeps with them for this repo, such as its memory notes. "
        "Nothing else is touched: not other projects, Claude's settings or history.jsonl, your "
        "data folder or ~/.diffdata. Close Claude Code before you go on."
    )
    while True:
        answer = ask(ASK).strip().lower()
        if answer == "yes":
            break
        if answer == "no":
            say(NOTHING_DELETED)
            return 0
        say("Please type yes or no.")
    deleted = 0
    for folder, checked in found.matched.items():
        try:
            why = delete(folder, checked, found)
        except OSError as e:
            name = shown(folder.name)
            say(f"Couldn't delete all of {name}: {type(e).__name__}. Close Claude Code.")
            continue
        if why:
            say(f"Left alone: {shown(folder.name)} ({why}).")
            continue
        deleted += 1
    say(f"Deleted {deleted} of {len(found.matched)} folder(s) of this repo's session history.")
    return 0 if deleted == len(found.matched) else 1


def offer(ask: Callable[[str], str], say: Callable[[str], None], **kwargs) -> None:
    """After a push that sent every passed copy: offer clean-history. Enter skips."""
    say(OFFER)
    while True:
        answer = ask(OFFER_ASK).strip().lower()
        if answer in ("", "no", "n"):
            say(LATER)
            return
        if answer == "yes":
            clean(ask, say, **kwargs)
            return
        say("Please type yes, or press Enter to skip.")
