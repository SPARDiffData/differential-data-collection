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
     is set (Claude Code sets it in the shells it runs). .claude/settings.json also denies it
     to Claude's shell tools
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
     can't read, is left alone. Just before deleting, each folder must still be the same
     folder (same identity) with the same sessions as on the list, or it's left alone
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
import shutil
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


def claude_folder() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def encode(path: PurePath | str) -> str:
    """A folder's path as Claude Code names its project folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


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


def inside_repo(cwd: str, repo: Path, roots: Iterable[str]) -> bool:
    """Whether a session's recorded folder is the repo or inside it. A folder that exists is
    compared by file identity (no link or letter case can fool it); one that's gone, such as
    a removed worktree, by its path."""
    try:
        path = Path(cwd)
        if not path.is_absolute():
            return False
        if path.exists():
            real = path.resolve()
            return any(os.path.samefile(p, repo) for p in (real, *real.parents) if p.exists())
    except (OSError, ValueError):
        return False
    return lexically_inside(cwd, roots)


def overlap(one: Path, other: Path) -> bool:
    """Whether one folder is the other or inside it, either way, after resolving links."""
    try:
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


def unsafe_inside(folder: Path) -> str | None:
    """Why deleting `folder` could reach outside it, or None: a link, a junction, a mount
    point, a file with other hard links, or a folder that can't be read."""
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=errors.append, followlinks=False):
        for name in dirnames:
            entry = Path(dirpath) / name
            if is_link(entry):
                return "it holds a link"
            if os.path.ismount(entry):
                return "it holds a mount point"
        for name in filenames:
            entry = Path(dirpath) / name
            if is_link(entry):
                return "it holds a link"
            try:
                if entry.lstat().st_nlink > 1:
                    return "it holds a file with other hard links"
            except OSError:
                return "it holds a file it can't read"
    if errors:
        return "it holds a folder it can't read"
    return None


def transcripts(folder: Path) -> list[Path]:
    """Every transcript in a project folder, at any depth: sessions, subagents' and the
    .jsonl.superseded-* copies."""
    return sorted(p for p in folder.rglob("*.jsonl*") if p.is_file() and not p.is_symlink())


@dataclass
class Found:
    projects: Path
    repo: Path
    roots: tuple[str, ...]
    keep: list[Path]
    matched: dict[Path, tuple[int, tuple]] = field(default_factory=dict)  # folder -> count, id
    left: dict[Path, str] = field(default_factory=dict)  # folder -> why it was left alone


def identity(folder: Path) -> tuple:
    info = folder.stat(follow_symlinks=False)
    return (info.st_dev, info.st_ino)


def check(folder: Path, found: Found) -> tuple[int, tuple] | str:
    """A matched folder's number of sessions and identity, or why it's left alone."""
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
    sessions = sorted(folder.glob("*.jsonl"))
    if not sessions:
        return "no session files to check"
    for transcript in transcripts(folder):
        cwd = session_cwd(transcript)
        if cwd is None:
            return "a session with no recorded folder"
        if not inside_repo(cwd, found.repo, found.roots):
            return "a session from another folder"
    return len(sessions), identity(folder)


def find(repo: Path | None = None, data: Path | None = None, projects: Path | None = None) -> Found:
    """This repo's project folders, checked (must-refuse 6 and 7)."""
    repo = (repo or REPO).absolute()
    projects = projects if projects is not None else claude_folder() / "projects"
    keep = [Path.home() / ".diffdata", *([data] if data is not None else [])]
    for part in (projects.parent, projects):
        if is_link(part):
            raise HistoryError(f"{part} is a link, so clean-history won't delete under it.")
    for other in keep:
        if overlap(projects, other):
            raise HistoryError(
                f"Claude's projects folder overlaps {other}, so clean-history won't delete there."
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


def clean(
    ask: Callable[[str], str],
    say: Callable[[str], None] = print,
    repo: Path | None = None,
    data: Path | None = None,
    projects: Path | None = None,
) -> int:
    """Show this repo's project folders, ask for the typed word yes, then delete them."""
    try:
        found = find(repo, data, projects)
    except HistoryError as e:
        say(f"{e} {NOTHING_DELETED}")
        return 1
    for folder, why in found.left.items():
        say(f"Left alone: {folder.name} ({why}).")
    if not found.matched:
        say(f"No Claude Code session history for this repo in {found.projects}. {NOTHING_DELETED}")
        return 0
    say(f"Claude Code keeps this repo's session history in {found.projects}. It will delete:")
    for folder, (count, _) in found.matched.items():
        say(f"  {folder.name}: {count} session(s)")
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
    for folder, shown in found.matched.items():
        # Again, just before: the same folder, with the same sessions, still safe to delete.
        if check(folder, found) != shown:
            say(f"Left alone: {folder.name} (it changed while you read the list).")
            continue
        try:
            shutil.rmtree(folder)
        except OSError as e:
            say(f"Couldn't delete all of {folder.name}: {type(e).__name__}. Close Claude Code.")
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
