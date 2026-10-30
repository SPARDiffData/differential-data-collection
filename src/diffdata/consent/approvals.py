"""The approvals file (CON-1, CON-3): what a person decided about each new canvas or Google
file the reference log found, typed in their own terminal (consent/review.py).

A source is read if config.toml lists it, or its latest decision here is "approved". The
reference log and the Slack export read this file; only consent/review.py writes it.

approvals.jsonl sits next to config.toml, in ~/.diffdata/. One decision per line, only ever
added to, and the latest line for a source wins:
  {"key": "slack-file:F...", "decision": "approved", "at": "2026-10-28T20:00:00Z", "answer": "all"}
It holds keys, decisions, times and the answer as typed, never titles, names or link text.
Its keys are file IDs, which identify a doc, so the file is private, like config.toml.

It's never written inside a git repo, or in a folder whose name shows a sync app, checked
after following any shortcut. A file diffdata can't read is never written to, so no
earlier skip or withdrawal is lost, and the export then reads only config.toml's canvases.
"""

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from diffdata.common.config import Config
from diffdata.common.paths import find_git_root, synced_by

APPROVED = "approved"
SKIPPED = "skipped"
WITHDRAWN = "withdrawn"
DECISIONS = (APPROVED, SKIPPED, WITHDRAWN)
FIELDS = ("key", "decision", "at", "answer")
# Only canvases and Google files are ever offered for review, so only their keys are kept.
KEY = re.compile(r"slack-file:F[A-Z0-9]{6,}|google:[A-Za-z0-9_-]{10,}")
CANVAS_PREFIX = "slack-file:"
BUSY = (
    "Another review or withdraw is recording its answer right now, so nothing was recorded. "
    "Run this again. If none is running, delete approvals.jsonl.lock next to config.toml."
)


class ApprovalsError(Exception):
    """A decision can't be shown or recorded. The message says why, with no IDs or titles."""


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold the approvals file for one last check and write, so two answers recorded at the
    same moment can't undo each other: a review writing a stale copy over a withdrawal would
    bring the approval back. If another holds it, refuse rather than wait."""
    lock = path.with_name(path.name + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError as e:
        raise ApprovalsError(BUSY) from e
    except OSError as e:
        raise ApprovalsError("Couldn't write next to config.toml, so nothing was recorded.") from e
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def approvals_file(config: Config) -> Path | None:
    """approvals.jsonl, next to the config.toml these settings came from."""
    return config.path.parent / "approvals.jsonl" if config.path else None


def readable(row) -> bool:
    return (
        isinstance(row, dict)
        and set(FIELDS) <= row.keys()
        and isinstance(row["key"], str)
        and KEY.fullmatch(row["key"]) is not None
        and isinstance(row["decision"], str)
        and row["decision"] in DECISIONS
        and isinstance(row["at"], str)
        and isinstance(row["answer"], str)
    )


def read_decisions(path: Path | None) -> dict[str, str] | None:
    """Each source's latest decision, by key. Empty if there's no file yet, and None if
    diffdata can't read it."""
    if path is None or not path.exists():
        return {}
    decisions: dict[str, str] = {}
    try:
        # Only \n ends a line, as in the stores (#74).
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            if not readable(row):
                return None
            decisions[row["key"]] = row["decision"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decisions


def approved_canvases(config: Config) -> list[str] | None:
    """The canvas IDs whose latest decision is "approved", or None if diffdata can't read
    the approvals file."""
    decisions = read_decisions(approvals_file(config))
    if decisions is None:
        return None
    return sorted(
        key.removeprefix(CANVAS_PREFIX)
        for key, decision in decisions.items()
        if decision == APPROVED and key.startswith(CANVAS_PREFIX)
    )


def safe_file(config: Config) -> Path:
    """Where to write approvals.jsonl, after following any shortcut. Refuses a place inside
    a git repo, or in a folder whose name shows a sync app (best effort, as for the data
    folder)."""
    path = approvals_file(config)
    if path is None:
        raise ApprovalsError("Couldn't tell where config.toml is, so nothing was recorded.")
    real = path.resolve()
    for candidate in (path.absolute(), real):
        if find_git_root(candidate) is not None or synced_by(candidate) is not None:
            raise ApprovalsError(
                "The approvals file goes next to config.toml, and that folder is inside a git "
                "repository or a folder a sync app looks after, so nothing was recorded. Move "
                "config.toml to the .diffdata folder in your home folder, then run this again."
            )
    return real


def record(path: Path, decisions: list[tuple[str, str]], answer: str, now: datetime) -> None:
    """Add one line per (key, decision) to the approvals file, through a temporary file, so
    a write cut short leaves the old file whole. The caller holds `locked()`, and has
    checked the file is safe to write and can be read."""
    try:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as e:
        raise ApprovalsError("Couldn't read the approvals file, so nothing was recorded.") from e
    if old and not old.endswith("\n"):
        old += "\n"
    at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        dict(zip(FIELDS, (key, decision, at, answer), strict=True)) for key, decision in decisions
    ]
    body = (old + "".join(json.dumps(row) + "\n" for row in rows)).encode("utf-8")
    # The same as the reference log's write_log(), which this module can't import: the
    # reference log reads the approvals.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".saving-")
        try:
            with os.fdopen(handle, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise ApprovalsError("Couldn't write the approvals file, so nothing was recorded.") from e
