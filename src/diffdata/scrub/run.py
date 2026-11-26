"""Make the scrubbed copies (SCR-2): every opted-in Slack day file, canvas clean copy and
Google Doc, with people, Slack IDs and emails replaced. The originals are only ever read.

Under the data folder:
  scrubbed/slack/export/<channel ID>/<YYYY-MM-DD>.md
  scrubbed/documents/current/slack-canvas_<canvas ID>.md
  scrubbed/documents/current/google-doc_<doc ID>.md    a doc's current version (#147)
  scrubbed/documents/comments/google-doc_<doc ID>.md   its comments (scrub/docs.py)
  scrubbed/manifest.json   each copy with its hash and checks, and the copies held back
  private/people.toml      who's who (people.py), never in a scrubbed copy
  private/people-numbers.json  every [Person N] given, to catch someone lost from the list
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from diffdata import __version__
from diffdata.collect import slack_canvas
from diffdata.common.config import Config
from diffdata.common.documents import document_dir, documents_dir
from diffdata.consent import approvals
from diffdata.discover import slack_state
from diffdata.discover.slack_export import read_inbox, read_store, saved_records
from diffdata.discover.slack_inbox import (
    SlackExportError,
    applied_stamp,
    inbox_dir,
    run_channel_names,
    slack_dir,
)
from diffdata.discover.slack_joins import is_held, late_joiners, left_out_stamp
from diffdata.scrub import docs
from diffdata.scrub.people import (
    NUMBERS_FILE,
    PEOPLE_FILE,
    People,
    PeopleError,
    add_seen,
    check_whole,
    numbers,
    read_given,
    read_people,
    render,
    seen_in_slack,
)
from diffdata.scrub.secrets import RULES_RELEASE, RulesError, rules
from diffdata.scrub.text import (
    CHECKS,
    MAYBE_SECRET,
    SHARED,
    UNKNOWN_GOOGLE,
    UNKNOWN_ID,
    UNKNOWN_MENTION,
    Flag,
    Scrubber,
    key,
)

SCRUBBED = "scrubbed"
IN_PROGRESS = "scrubbed-in-progress"
PRIVATE = "private"
LOCK = "scrub.lock"
FLAGS = "flags.md"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
SAFE_ID = re.compile(r"[A-Z0-9]+")
CHANNEL_LINE = re.compile(r"^channel_id: (\S+)$", re.MULTILINE)
# A Google Doc's ID, as config.py and the approvals file take it.
DOC_ID = re.compile(r"[A-Za-z0-9_-]{20,}")
# Every folder and file a scrub makes in scrubbed/ and scrubbed-in-progress/, the folders
# it replaces, including a temporary file a run cut short left where it writes. Anything
# else stops the run, except what Finder and File Explorer add to a folder someone opens.
FOLDERS = re.compile(r"slack(?:/export(?:/[A-Z0-9]+)?)?|documents(?:/current|/comments)?")
MADE = re.compile(
    r"manifest\.json|slack/export/[A-Z0-9]+/\d{4}-\d{2}-\d{2}\.md"
    r"|documents/current/slack-canvas_[A-Z0-9]+\.md"
    r"|documents/(?:current|comments)/google-doc_[A-Za-z0-9_-]{20,}\.md"
    r"|(?:(?:slack/export/[A-Z0-9]+|documents/current|documents/comments)/)?\.writing-[^/]+"
)
SYSTEM_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
FLAG_KINDS = {
    SHARED: "A name more than one person has, hidden as [name]",
    UNKNOWN_MENTION: "A mention of someone not in the list, hidden as @[name?]",
    UNKNOWN_ID: "A Slack ID of someone not in the list, hidden as [name?]",
    UNKNOWN_GOOGLE: "A Google person ID (perm: or gaia:), hidden as [name?]",
    MAYBE_SECRET: "Text that looks random, maybe a key or password, hidden as [secret?]",
}
# What's checked and what isn't, for anyone who reads the manifest (Paul, #117).
NOTE = (
    "Not approved for release. Replaced: secrets (the rules of "
    f"{RULES_RELEASE}, run in Python, and a randomness check), meeting links, phone "
    "numbers, emails, Slack IDs, mentions and the names in the people list. Google Docs: "
    "the current version and the comments, with each comment's author from the people "
    "list and comments by people who opted out left out; their versions and suggestions "
    "aren't sent yet. Not checked yet: names of people who aren't in the people list, and "
    "links that open the original (a doc shared with anyone who has the link, a Slack file)."
)
FLAGS_HEADER = """\
# Flags from the last scrub

The scrub hid these without knowing who they are. Each one is real text from your data,
so this file stays in private/ and is never shared.

To resolve one, edit people.toml: add it to someone's nicknames (or a Slack ID to their
slack_ids) if it's them, or to not_names if it isn't a name. The next scrub applies it.

A possible secret ([secret?]) is listed by copy only, never by its value. It stays hidden
either way; look at the original if you need to know what it was."""


class ScrubError(Exception):
    """The scrub can't go ahead. The message names no person and quotes no text."""


@dataclass(frozen=True)
class Source:
    # "slack" for a day file, "slack-canvas" for a canvas clean copy, "google-doc" for a
    # doc's clean copy, or "google-doc-comments" for its events.jsonl
    kind: str
    id: str  # the channel ID and day, or the canvas or doc ID
    original: Path
    copy: str  # where its copy goes, under scrubbed/


@dataclass(frozen=True)
class OptedIn:
    channels: Callable[[str, str], bool]  # a test on a channel's ID and name
    canvases: frozenset[str]
    docs: frozenset[str]
    approvals_read: bool  # False if the approvals file couldn't be read


def opted_in(config: Config) -> OptedIn:
    """What gets a scrubbed copy, and what push may send (CON-1, CON-3): the channels listed
    in config.toml right now, by name or by ID, and the canvases and Google Docs listed
    there or whose latest decision in the approvals file is "approved". A skipped, withdrawn
    or undecided one gets none, and its earlier copy is gone after the next run. If the
    approvals file can't be read, only the listed ones count. This rule lives only here."""
    canvases, docs_ = set(config.slack_canvases), set(config.google_docs)
    decisions = approvals.read_decisions(approvals.approvals_file(config))
    for found in approvals.approved_keys(decisions or {}):
        if found.startswith(approvals.CANVAS_PREFIX):
            canvases.add(found.removeprefix(approvals.CANVAS_PREFIX))
        elif found.startswith(approvals.GOOGLE_PREFIX):
            docs_.add(found.removeprefix(approvals.GOOGLE_PREFIX))
    return OptedIn(
        config.lists_channel, frozenset(canvases), frozenset(docs_), decisions is not None
    )


def sources(config: Config, opted: OptedIn | None = None) -> tuple[list[Source], int]:
    """The originals that get a copy, and how many clean copies were skipped because they
    aren't opted in, or aren't from a source the scrub knows."""
    data = config.data_dir
    opted = opted or opted_in(config)
    channels, canvases = opted.channels, opted.canvases
    found = []
    for channel_id, name in sorted(run_channel_names(data).items()):
        if not channels(channel_id, name) or not SAFE_ID.fullmatch(channel_id):
            continue
        # The view's folder, named as slack_export.build names it.
        folder = slack_dir(data) / "export" / re.sub(r"[^a-z0-9_-]", "-", name.lower())
        for day in sorted(folder.glob("*.md")):
            if DAY.fullmatch(day.stem):
                copy = f"slack/export/{channel_id}/{day.name}"
                found.append(Source("slack", f"{channel_id}/{day.stem}", day, copy))
    skipped, with_comments = 0, set()
    for path in sorted((documents_dir(data) / "current").glob("*.md")):
        # <source>_<title>_<id>.md: neither the source nor the title holds a _
        parts = path.stem.split("_", 2)
        if len(parts) == 3 and parts[0] == slack_canvas.SOURCE and parts[2] in canvases:
            copy = f"documents/current/{slack_canvas.SOURCE}_{parts[2]}.md"
            found.append(Source(slack_canvas.SOURCE, parts[2], path, copy))
        elif (
            len(parts) == 3
            and parts[0] == docs.SOURCE
            and parts[2] in opted.docs
            and DOC_ID.fullmatch(parts[2])
        ):
            doc_id = parts[2]
            copy = f"documents/current/{docs.SOURCE}_{doc_id}.md"
            found.append(Source(docs.SOURCE, doc_id, path, copy))
            events = document_dir(data, docs.SOURCE, doc_id) / "events.jsonl"
            # Two clean copies of one doc (an old title left behind) are both held back as
            # a clash; its comments get one copy either way.
            if events.is_file() and doc_id not in with_comments:
                with_comments.add(doc_id)
                comments = f"documents/comments/{docs.SOURCE}_{doc_id}.md"
                found.append(Source(docs.COMMENTS, doc_id, events, comments))
        else:
            skipped += 1
    return found, skipped


def scrub(config: Config) -> str:
    """Make every scrubbed copy, all or nothing. Returns the report, which gives counts only."""
    data = config.data_dir
    if not data.is_dir():
        raise ScrubError("The data folder doesn't exist yet. Run /slack-export first.")
    try:
        rules()  # a missing or changed rule file stops the run before anything is touched
    except RulesError as e:
        raise ScrubError(str(e)) from None
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
            f"Another scrub or a push is running, or a scrub stopped without finishing. If "
            f"neither is running, delete {PRIVATE}/{LOCK} in your data folder and run again."
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


def saved_left_out(config: Config) -> tuple[dict[str, set[str]], dict[str, tuple], str]:
    """Who the build leaves out, worked out from the saved data the same way the build does,
    so a join saved since the last build, or a changed notice link, is caught too (#118).
    Returns each channel's late joiners, where its held-back messages start, and the stamp a
    build that applied all of it writes. The push checks the same stamp (store/push.py)."""
    data = config.data_dir
    channel_dirs = sorted(p for p in inbox_dir(data).glob("*") if p.is_dir())
    try:
        inbox = {d.name: read_inbox(d, d.name, config.slack_workspace_url) for d in channel_dirs}
        late, held_after, _ = late_joiners(
            config,
            saved_records(data, inbox),
            run_channel_names(data),
            slack_state.start_points(inbox_dir(data))[0],
        )
    except SlackExportError as e:
        raise ScrubError(str(e)) from e
    return late, held_after, left_out_stamp(set(config.slack_opted_out), late, held_after)


def make_copies(config: Config, earlier: bool) -> str:
    data = config.data_dir
    opted_out = set(config.slack_opted_out)
    stores = sorted((slack_dir(data) / "store").glob("*/messages.jsonl"))
    late, held_after, expected = saved_left_out(config)
    # Read once, before anything is checked or copied, and written into the manifest as it
    # was read: a build finishing during the scrub then makes the manifest stale, never
    # current (store/push.py compares the two).
    stamp = applied_stamp(data)
    if stores and stamp != expected:
        # The last finished build applied a different list, or none, or stopped partway.
        raise ScrubError(
            "slack_opted_out or slack_notices in config.toml has changed since the last "
            "build, a new export or a join has been saved since then, or the last build "
            "didn't finish, so nothing was scrubbed. Run uv run diffdata discover slack build "
            "first, then scrub again."
        )
    for store in stores:
        channel_id = store.parent.name
        gone = opted_out | late.get(channel_id, set())
        if any(
            record["user_id"] in gone or is_held(record, held_after, channel_id)
            for record in read_store(store)
        ):
            # The build is what leaves them out (#105), so it has to run first.
            raise ScrubError(
                "The saved Slack data still has messages the build leaves out (by people in "
                "slack_opted_out, by people who joined after a notice, or newer than the last "
                "complete export), so nothing was scrubbed. Run "
                "uv run diffdata discover slack build first, then scrub again."
            )
    people_file, numbers_file = data / PEOPLE_FILE, data / NUMBERS_FILE
    given = read_given(numbers_file)
    if people_file.is_file():
        people = read_people(people_file)
        check_whole(people, given)
    elif earlier:
        raise ScrubError(
            f"{PEOPLE_FILE} is missing, but {PRIVATE}/ is there from an earlier scrub, so the "
            f"scrub stopped rather than give everyone a new number. Put the list back, or "
            f"delete the whole {PRIVATE}/ folder to start a new list on purpose."
        )
    else:
        people = People()
    before = render(people)
    try:
        seen = seen_in_slack(data)
    except SlackExportError as e:
        raise ScrubError(str(e)) from e
    new = add_seen(people, seen)
    opted = opted_in(config)
    found, skipped = sources(config, opted)
    # Each opted-in doc's comment authors join the list by name (#147, scrub/docs.py). A doc
    # whose comments can't be read adds no one, and its comments copy is held back below.
    comment_rows = read_comments(data, found)
    authors = {
        name
        for rows in comment_rows.values()
        for row in rows or []
        if (name := docs.author_name(row)) is not None
    }
    gone = docs.opted_out_names(people, opted_out)
    new += docs.add_authors(people, {name for name in authors if key(name) not in gone})
    if render(people) != before or not people_file.is_file():
        write_file(people_file, render(people))
    # After the list, so a run cut short between the two never records a number the list
    # doesn't have.
    if set(numbers(people)) != given or not numbers_file.is_file():
        write_file(numbers_file, json.dumps({"numbers": numbers(people)}) + "\n")

    scrubber = Scrubber(people)
    building = data / IN_PROGRESS
    remove(building)  # anything a run cut short left
    building.mkdir()
    clashes = Counter(source.copy for source in found)
    copies, held, flags = [], [], []
    left_out = 0
    for source in found:
        if clashes[source.copy] > 1:
            scrubbed, why, found_flags = None, "two originals would make this one copy", []
        else:
            scrubbed, why, found_flags, dropped = make_copy(
                source, data, people, scrubber, opted_out, comment_rows
            )
            left_out += dropped
        if why is not None:
            held.append({"source": source.kind, "id": source.id, "why": why})
            continue
        if scrubbed is None:
            continue  # a doc with no comments has no comments copy
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
        "note": NOTE,
        # Who the build had left out when these copies were made (opt-outs, late joiners and
        # held-back points), so a push can tell if that changed since (store/push.py). A
        # fingerprint: it names nobody. The field keeps its first name.
        "opt_out_stamp": stamp,
        "copies": copies,
        "held_back": held,
    }
    write_file(building / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    swap(data)
    write_file(data / PRIVATE / FLAGS, render_flags(flags))

    kinds = Counter(copy["source"] for copy in copies)
    with_docs = ""
    if kinds[docs.SOURCE] or kinds[docs.COMMENTS]:
        with_docs = (
            f", {kinds[docs.SOURCE]} Google Doc(s) and {kinds[docs.COMMENTS]} Google Doc "
            "comments copy(ies)"
        )
    report = [
        f"{len(copies)} scrubbed copy(ies): {kinds['slack']} Slack day file(s)"
        f"{',' if with_docs else ' and'} {kinds[slack_canvas.SOURCE]} canvas(es){with_docs}, "
        f"with {len(flags)} flag(s) for review."
    ]
    if flags:
        report.append(f"What was flagged is in {PRIVATE}/{FLAGS} in your data folder.")
    if held:
        report.append(
            f"{len(held)} copy(ies) held back, with no file written: see "
            f"{SCRUBBED}/manifest.json for why."
        )
    if left_out:
        report.append(
            f"{left_out} Google Doc comment(s) or reply(ies) by people who opted out were left out."
        )
    if skipped:
        report.append(
            f"{skipped} clean copy(ies) of canvases or docs not listed in config.toml or "
            "approved at review now skipped."
        )
    if not opted.approvals_read:
        report.append(
            "Couldn't read the approvals file next to config.toml, so only the canvases and "
            "docs listed in config.toml got a copy."
        )
    report.append(f"The people list has {len(people.people)} people, {new} of them new.")
    report.append(
        "These copies are not approved for release: names of people who aren't in the "
        "people list, and links that open the original, aren't checked yet. Read your "
        "copies before you push."
    )
    return "\n".join(report)


def read_comments(data: Path, found: list[Source]) -> dict[str, list[dict] | None]:
    """Each doc's comment rows, by doc ID, for the docs among `found` with an events.jsonl:
    None for one whose events.jsonl can't be read."""
    rows: dict[str, list[dict] | None] = {}
    for source in found:
        if source.kind == docs.COMMENTS:
            try:
                rows[source.id] = docs.comment_rows(data, source.id) or []
            except docs.DocError:
                rows[source.id] = None
    return rows


def make_copy(
    source: Source,
    data: Path,
    people: People,
    scrubber: Scrubber,
    opted_out: set[str],
    comment_rows: dict[str, list[dict] | None],
) -> tuple[str | None, str | None, list[Flag], int]:
    """One source's scrubbed copy. Returns its text (None if it gets no copy and isn't held
    back: a doc with no comments), why it's held back (None if it isn't), its flags, and how
    many comments were left out because their author opted out. Push uses this too, to check a
    doc's copies are what a scrub makes from the doc now."""
    author_flags, dropped = [], 0
    if source.kind == docs.COMMENTS:
        rows, meta = comment_rows.get(source.id), read_meta(data, source.id)
        if rows == []:
            return None, None, [], 0
        if rows is None or meta is None:
            return None, "its comments can't be read", [], 0
        text, author_flags, dropped = docs.comments_copy(source.id, meta, rows, people, opted_out)
    else:
        try:
            text = source.original.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return None, "it isn't UTF-8 text", [], 0
    if source.kind == "slack":
        channel = CHANNEL_LINE.search(text.partition("\n---\n")[0])
        if channel is None or channel[1] != source.id.split("/")[0]:
            return None, "its header doesn't name its channel", [], 0
        scrubbed, found = scrubber.scrub(text)
        left = scrubber.leftovers(scrubbed)
    else:
        # A document: its header read strictly, its title and body scrubbed on their own.
        header = docs.split(text)
        if header is None or not docs.names_itself(header, source.kind, source.id):
            what = "canvas" if source.kind == slack_canvas.SOURCE else "doc"
            return None, f"its header doesn't name its {what}", [], 0
        if source.kind == docs.SOURCE and comment_rows.get(source.id, []) is None:
            # Its commenters weren't learned, so a name of theirs could be left in the text.
            return None, "its comments can't be read", [], 0
        title, found = docs.title(header), []
        if title is not None:
            title, found = scrubber.scrub(title)
        body, body_flags = scrubber.scrub(header.body)
        found += body_flags
        scrubbed = docs.join(docs.with_title(header, title, body))
        left = sorted(set(scrubber.leftovers(title or "")) | set(scrubber.leftovers(body)))
    if left:
        return None, "the last check found " + " and ".join(left), [], dropped
    return scrubbed, None, author_flags + found, dropped


def read_meta(data: Path, doc_id: str) -> dict | None:
    """A doc's meta.json (common/documents.py), or None if it can't be read."""
    path = document_dir(data, docs.SOURCE, doc_id) / "meta.json"
    try:
        meta = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


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
            # quoted, so any text stays one line. A possible secret is never written down.
            text = "[secret?]" if kind == MAYBE_SECRET else json.dumps(shown, ensure_ascii=False)
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
    """Stop if a folder the scrub replaces holds a file or folder a scrub doesn't make:
    replacing the folder would delete it."""
    folder = data / name

    def made(path: Path) -> bool:
        where = path.relative_to(folder).as_posix()
        if path.is_dir():
            return FOLDERS.fullmatch(where) is not None
        parent = path.parent.relative_to(folder).as_posix()
        system = path.name in SYSTEM_FILES and (parent == "." or FOLDERS.fullmatch(parent))
        return bool(system or MADE.fullmatch(where))

    # Its name isn't given: someone else's file could be named after a person.
    if not all(made(path) for path in folder.rglob("*")):
        raise ScrubError(
            f"{name}/ in your data folder holds a file or folder the scrub didn't make, so it "
            f"stopped without changing anything. A scrub replaces all of {name}/, so move it "
            "out, and run again. Hidden files count too."
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
