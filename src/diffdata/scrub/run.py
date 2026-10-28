"""Make the scrubbed copies (SCR-2): every opted-in Slack day file and canvas clean copy, with
people, Slack IDs and emails replaced. The originals are only ever read.

Under the data folder:
  scrubbed/slack/export/<channel ID>/<YYYY-MM-DD>.md
  scrubbed/documents/current/slack-canvas_<canvas ID>.md
  scrubbed/manifest.json   each copy with its hash and checks, and the copies held back
  private/people.toml      who's who (people.py), never in a scrubbed copy
  private/flags.md         what was hidden without knowing who it is, as written
  private/scrub.lock       while a scrub runs, so two can't run at once

File names hold IDs only, so no name can hide in a path. A run is all or nothing: it builds
every copy in scrubbed-in-progress/ and swaps that in for scrubbed/ only once every copy is
done. If anything fails, both folders and the flags are removed, so an old copy of a source
that's no longer opted in can't linger. A copy the last check finds anything left in isn't written:
the manifest lists it as held back.

A scrub writes only in scrubbed/, scrubbed-in-progress/ and private/. If any of them is a
link or holds one, it stops before writing anything, and so it does if scrubbed/ or
scrubbed-in-progress/, which it replaces whole, holds a file a scrub doesn't make. Every
file is written to a temporary file and renamed into place, so a hard link is replaced,
never written through.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from diffdata import __version__
from diffdata.collect import slack_canvas
from diffdata.common.config import Config
from diffdata.common.documents import documents_dir
from diffdata.discover.slack_inbox import run_channel_names, slack_dir
from diffdata.scrub.people import (
    PEOPLE_FILE,
    People,
    PeopleError,
    add_seen,
    read_people,
    render,
    seen_in_slack,
)
from diffdata.scrub.text import CHECKS, SHARED, UNKNOWN_ID, UNKNOWN_MENTION, Flag, Scrubber

SCRUBBED = "scrubbed"
IN_PROGRESS = "scrubbed-in-progress"
PRIVATE = "private"
LOCK = "scrub.lock"
FLAGS = "flags.md"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
SAFE_ID = re.compile(r"[A-Z0-9]+")
CHANNEL_LINE = re.compile(r"^channel_id: (\S+)$", re.MULTILINE)
# Every file a scrub makes in scrubbed/ and scrubbed-in-progress/, the folders it replaces,
# including a temporary file a run cut short left. Anything else stops the run.
MADE = re.compile(
    r"manifest\.json|slack/export/[A-Z0-9]+/\d{4}-\d{2}-\d{2}\.md"
    r"|documents/current/slack-canvas_[A-Z0-9]+\.md|(?:.+/)?\.writing-[^/]+"
)
FLAG_KINDS = {
    SHARED: "A name more than one person has, hidden as [name]",
    UNKNOWN_MENTION: "A mention of someone not in the list, hidden as @[name?]",
    UNKNOWN_ID: "A Slack ID of someone not in the list, hidden as [name?]",
}
FLAGS_HEADER = """\
# Flags from the last scrub

The scrub hid these without knowing who they are. Each one is real text from your data,
so this file stays in private/ and is never shared.

To resolve one, edit people.toml: add it to someone's nicknames (or a Slack ID to their
slack_ids) if it's them, or to not_names if it isn't a name. The next scrub applies it."""


class ScrubError(Exception):
    """The scrub can't go ahead. The message names no person and quotes no text."""


@dataclass(frozen=True)
class Source:
    kind: str  # "slack" for a day file, or "slack-canvas" for a canvas clean copy
    id: str  # the channel ID and day, or the canvas ID
    original: Path
    copy: str  # where its copy goes, under scrubbed/


def opted_in(config: Config) -> tuple[set[str], set[str]]:
    """The channels (by name) and canvases (by ID) that get a scrubbed copy: the ones listed
    in config.toml right now (CON-1). Anything else gets none, and its earlier copy is gone
    after the next run. This rule lives only here, so the approval step can add to it."""
    return set(config.slack_channels), set(config.slack_canvases)


def sources(config: Config) -> tuple[list[Source], int]:
    """The originals that get a copy, and how many other clean copies were skipped: Google
    Docs aren't scrubbed yet."""
    data = config.data_dir
    channels, canvases = opted_in(config)
    found = []
    for channel_id, name in sorted(run_channel_names(data).items()):
        if name not in channels or not SAFE_ID.fullmatch(channel_id):
            continue
        # The view's folder, named as slack_export.build names it.
        folder = slack_dir(data) / "export" / re.sub(r"[^a-z0-9_-]", "-", name.lower())
        for day in sorted(folder.glob("*.md")):
            if DAY.fullmatch(day.stem):
                copy = f"slack/export/{channel_id}/{day.name}"
                found.append(Source("slack", f"{channel_id}/{day.stem}", day, copy))
    skipped = 0
    for path in sorted((documents_dir(data) / "current").glob("*.md")):
        # <source>_<title>_<id>.md: neither the source nor the title holds a _
        parts = path.stem.split("_", 2)
        if parts[0] != slack_canvas.SOURCE:
            skipped += 1
        elif len(parts) == 3 and parts[2] in canvases:
            copy = f"documents/current/{slack_canvas.SOURCE}_{parts[2]}.md"
            found.append(Source(slack_canvas.SOURCE, parts[2], path, copy))
    return found, skipped


def scrub(config: Config) -> str:
    """Make every scrubbed copy, all or nothing. Returns the report, which gives counts only."""
    data = config.data_dir
    if not data.is_dir():
        raise ScrubError("The data folder doesn't exist yet. Run /slack-export first.")
    for name in (SCRUBBED, IN_PROGRESS, PRIVATE):
        refuse_links(data, name)
    for name in (SCRUBBED, IN_PROGRESS):
        refuse_others(data, name)
    private = data / PRIVATE
    earlier = private.is_dir() and any(private.iterdir())
    private.mkdir(exist_ok=True)
    try:
        (private / LOCK).touch(exist_ok=False)
    except FileExistsError:
        raise ScrubError(
            f"Another scrub is running, or one stopped without finishing. If none is "
            f"running, delete {PRIVATE}/{LOCK} in your data folder and run again."
        ) from None
    try:
        return make_copies(config, earlier)
    except Exception as e:
        raise ScrubError(stopped(e, clear(data))) from e
    except BaseException:  # Ctrl+C
        clear(data)
        raise
    finally:
        (private / LOCK).unlink(missing_ok=True)


def make_copies(config: Config, earlier: bool) -> str:
    data = config.data_dir
    people_file = data / PEOPLE_FILE
    if people_file.is_file():
        people = read_people(people_file)
    elif earlier:
        raise ScrubError(
            f"{PEOPLE_FILE} is missing, but {PRIVATE}/ is there from an earlier scrub, so the "
            f"scrub stopped rather than give everyone a new number. Put the list back, or "
            f"delete the whole {PRIVATE}/ folder to start a new list on purpose."
        )
    else:
        people = People()
    before = render(people)
    new = add_seen(people, seen_in_slack(data))
    if render(people) != before or not people_file.is_file():
        write_file(people_file, render(people))

    scrubber = Scrubber(people)
    building = data / IN_PROGRESS
    remove(building)  # anything a run cut short left
    building.mkdir()
    found, skipped = sources(config)
    clashes = Counter(source.copy for source in found)
    copies, held, flags = [], [], []
    for source in found:
        why = None
        try:
            text = source.original.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            why = "it isn't UTF-8 text"
        if why is None and clashes[source.copy] > 1:
            why = "two originals would make this one copy"
        if why is None and source.kind == "slack":
            channel = CHANNEL_LINE.search(text.partition("\n---\n")[0])
            if channel is None or channel[1] != source.id.split("/")[0]:
                why = "its header doesn't name its channel"
        if why is None:
            scrubbed, found_flags = scrubber.scrub(text)
            if left := scrubber.leftovers(scrubbed):
                why = "the last check found " + " and ".join(left)
        if why is not None:
            held.append({"source": source.kind, "id": source.id, "why": why})
            continue
        write_file(building / source.copy, scrubbed)
        copies.append(
            {
                "source": source.kind,
                "id": source.id,
                "path": source.copy,
                "sha256": hashlib.sha256(scrubbed.encode("utf-8")).hexdigest(),
                "checks": list(CHECKS),
                "flags": len(found_flags),
            }
        )
        flags += [(source.copy, flag) for flag in found_flags]

    manifest = {
        "tool": f"diffdata {__version__}",
        "approved_for_release": False,
        "note": (
            "Not approved for release. Names, Slack IDs and emails are replaced. Phone "
            "numbers, meeting links and secrets aren't checked yet."
        ),
        "copies": copies,
        "held_back": held,
    }
    write_file(building / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    swap(data)
    write_file(data / PRIVATE / FLAGS, render_flags(flags))

    kinds = Counter(copy["source"] for copy in copies)
    report = [
        f"{len(copies)} scrubbed copy(ies): {kinds['slack']} Slack day file(s) and "
        f"{kinds[slack_canvas.SOURCE]} canvas(es), with {len(flags)} flag(s) for review."
    ]
    if flags:
        report.append(f"What was flagged is in {PRIVATE}/{FLAGS} in your data folder.")
    if held:
        report.append(
            f"{len(held)} copy(ies) held back, with no file written: see "
            f"{SCRUBBED}/manifest.json for why."
        )
    if skipped:
        report.append(
            f"{skipped} clean copy(ies) from other sources, like Google Docs, skipped: they "
            "aren't scrubbed yet."
        )
    report.append(f"The people list has {len(people.people)} people, {new} of them new.")
    report.append(
        "These copies are not approved for release: phone numbers, meeting links and "
        "secrets aren't checked yet."
    )
    return "\n".join(report)


def render_flags(flags: list[tuple[str, Flag]]) -> str:
    """flags.md: each flag once, with the copies it's in and how often."""
    places: dict[Flag, Counter] = defaultdict(Counter)
    for copy, flag in flags:
        places[flag][copy] += 1
    lines = [FLAGS_HEADER]
    for kind, title in FLAG_KINDS.items():
        found = sorted((f for f in places if f.kind == kind), key=lambda f: (f.text, f.shared_by))
        if found:
            lines += ["", f"## {title}", ""]
        for flag in found:
            shown = ("@" if kind == UNKNOWN_MENTION else "") + flag.text
            text = json.dumps(shown, ensure_ascii=False)  # quoted, so any text stays one line
            shared = f", shared by {' and '.join(flag.shared_by)}" if flag.shared_by else ""
            where = ", ".join(f"{copy} ({n})" for copy, n in sorted(places[flag].items()))
            lines.append(f"- {text}{shared}: {where}")
    if not flags:
        lines += ["", "Nothing was flagged."]
    return "\n".join(lines) + "\n"


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def holds_link(folder: Path) -> bool:
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                return True
            if entry.is_dir(follow_symlinks=False) and holds_link(Path(entry.path)):
                return True
    return False


def refuse_links(data: Path, name: str) -> None:
    """Stop if a folder the scrub writes in is a link or holds one, or isn't a folder:
    writing there could change files somewhere else."""
    path = data / name
    if is_link(path) or (path.exists() and (not path.is_dir() or holds_link(path))):
        raise ScrubError(
            f"{name}/ in your data folder is a link, holds one, or isn't a folder, so the scrub "
            "stopped without writing anything. Writing there could change files somewhere "
            f"else. Make {name}/ an ordinary folder with no links in it, and run again."
        )


def refuse_others(data: Path, name: str) -> None:
    """Stop if a folder the scrub replaces holds a file a scrub doesn't make: replacing the
    folder would delete it."""
    folder = data / name
    files = [path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()]
    if not all(MADE.fullmatch(file) for file in files):
        raise ScrubError(
            f"{name}/ in your data folder holds a file the scrub didn't make, so it stopped "
            f"without changing anything. A scrub replaces all of {name}/, so move the file "
            "out, and run again."
        )


def write_file(path: Path, text: str) -> None:
    """Write `text` to a temporary file and rename it into place. A write cut short leaves the
    old file whole, and a hard link is replaced, never written through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def remove(path: Path) -> None:
    """Delete a folder or file the scrub made. shutil.rmtree removes a link inside a folder,
    never what it points to, and refuses a folder that is itself a link."""
    if path.is_dir() and not is_link(path):
        shutil.rmtree(path)
    elif path.is_file() and not is_link(path):
        path.unlink()


def swap(data: Path) -> None:
    """Put the new copies in place of scrubbed/. On Windows, a folder holding a file that's
    open somewhere can't be moved for a moment, so it tries a few times."""
    for attempt in range(3):
        try:
            remove(data / SCRUBBED)
            (data / IN_PROGRESS).rename(data / SCRUBBED)
            return
        except PermissionError:
            time.sleep(attempt + 1)
    raise ScrubError(
        f"Couldn't replace {SCRUBBED}/ in your data folder. Is a file in it open in another "
        "program? Close it and run again."
    )


def clear(data: Path) -> bool:
    """After a failure: remove every scrubbed copy and the flags. Returns whether it could."""
    try:
        for path in (data / SCRUBBED, data / IN_PROGRESS, data / PRIVATE / FLAGS):
            remove(path)
    except OSError:
        return False
    return True


def stopped(e: Exception, cleared: bool) -> str:
    """What to say when a run fails partway. An unexpected error's message could quote
    collected text, so only its type is named."""
    if isinstance(e, ScrubError | PeopleError):
        why = str(e)
    else:
        why = f"The scrub stopped on an unexpected {type(e).__name__}."
    if cleared:
        return f"{why} No scrubbed copies are left, so nothing half-checked can be shared."
    return (
        f"{why} It couldn't remove {SCRUBBED}/, so delete {SCRUBBED}/ and {IN_PROGRESS}/ in "
        "your data folder by hand before sharing anything."
    )
