"""Slack canvases (COL-2): each opted-in canvas's text, with a new version each time it changes.

/slack-export reads the canvases listed in config.toml with `slack_read_canvas`, after the
channels. The hook passes each read to `save_read()`, which saves it unchanged as the
canvas's raw file, but only if it shows the canvas that was asked for (CON-1). `build()`
turns every saved read into a version and writes the canvas in the shared document format
(common/documents.py), which keeps a new version only when the text changed (COL-9).

A read puts the whole canvas in Claude's context, so a canvas is read only when it's due
(COL-8). Before the reads, a files search of each channel shows every file's size. The
hook passes it to `save_search()`, which keeps the size of each listed canvas and drops
every other row before anything is written (CON-1). `due()` then says to read a canvas
when any of these holds:
  - it has never been read
  - it's missing from this run's search, or its last read has no size on record
  - its size differs from the size at its last read (not the last size seen, so a read
    that failed is tried again)
  - a Slackbot canvas-tab notice has named it since its last read
  - its last read is older than canvas_refresh_days in config.toml, because sizes are
    rounded to 0.1 KB and miss small edits

A read is JSON: `canvas_id`, `markdown_content` (the text), `section_id_mapping` and, when
there are unresolved comments, `comment_threads`. It has no title, edit time or author, so
the title is the latest text's first heading, and every version is our own snapshot. A read
is saved with its time to the microsecond, so two reads in the same second keep their
order. The section mapping and comment threads stay in raw for COL-3. Links to images,
Docs and other canvases stay links: nothing here fetches them.

Under the data folder:
  documents/slack-canvas/<canvas id>/raw/<run id>/slack_read_canvas-<call>.json
  slack/canvas-sizes/<run id>/search_files-<call>.json   listed canvases' sizes, nothing else
and the rest of the shared format, including the clean copy in documents/current/.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.common.documents import (
    Document,
    DocumentError,
    Version,
    document_dir,
    documents_dir,
    exact,
    keep_changes,
    save_raw,
    write_document,
)
from diffdata.discover.slack_parse import next_cursor, result_json

SOURCE = "slack-canvas"
TOOL = "slack_read_canvas"
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
# Canvases mention people as <@U...> and channels as <#C...>, with no name attached. Only
# these are changed: canvas text can hold other <...>, such as <br>, which stays as it is.
MENTION = re.compile(r"<([@#])([A-Z][A-Z0-9]+)(?:\|([^<>\n]*))?>")
# A files search's `results` text: "## Files (20 results)", then one block per file, from
# "### Result 1 of 20" to its "Content:" line, with one "Size: 26.5 KB" line and one
# "File ID: F..." line. Only the part before "Content:" is read, since a file's content
# could hold anything. A title could too, if it held line breaks, so a search is refused
# when a block has a second ID or size line, a value in another shape, or isn't one the
# count announced: one file's text mustn't pass as another file's ID or size (CON-1).
FILE_COUNT = re.compile(r"^## Files \((\d+) results?\)", re.MULTILINE)
FILE_BLOCK = re.compile(r"^### Result \d+ of \d+[ \t]*$", re.MULTILINE)
FILE_CONTENT = re.compile(r"^Content:", re.MULTILINE)
FILE_ID = re.compile(r"^File ID:(.*)$", re.MULTILINE)
FILE_SIZE = re.compile(r"^Size:(.*)$", re.MULTILINE)
ID_VALUE = re.compile(r"F[A-Z0-9]+")
SIZE_VALUE = re.compile(r"\d+(?:\.\d+)? ?[A-Za-z]{1,5}")  # like 26.5 KB or 244.4 MB
# Slackbot posts this in the channel when someone edits a canvas pinned as a channel tab.
TAB_NOTICE = re.compile(r"made updates to a canvas tab: (F[A-Z0-9]+)")
SLACKBOT = "USLACKBOT"
# Why a canvas is read, in the order the report lists them. #14 adds "full run".
NEVER_READ = "never read"
MISSING = "not in this run's files search"
NO_OLD_SIZE = "no size from its last read"
SIZE_CHANGED = "size changed"
NOTICE = "canvas-tab notice since its last read"
REFRESH = "last read too long ago"
REASONS = (NEVER_READ, MISSING, NO_OLD_SIZE, SIZE_CHANGED, NOTICE, REFRESH)


class CanvasError(Exception):
    """A canvas read can't be saved. The message says why, in plain words."""


def save_read(data: Path, run_id: str, canvas_id: str, record: dict, call: str) -> str:
    """Save one canvas read, unchanged, if it shows the canvas that was asked for.
    Returns the note for Claude, which gives counts only."""
    read = result_json(record["tool_response"])
    if read is None or read.get("canvas_id") != canvas_id:
        raise CanvasError(
            "This canvas read doesn't show the canvas that was asked for, so it wasn't saved "
            "(CON-1). Stop the export."
        )
    text = read.get("markdown_content")
    if not isinstance(text, str):
        raise CanvasError(
            "This canvas read has no text diffdata can find, so it wasn't saved. The "
            "connector's format may have changed. Stop the export."
        )
    body = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    try:
        save_raw(data, SOURCE, canvas_id, run_id, f"{TOOL}-{call}.json", body)
    except (OSError, DocumentError) as e:
        raise CanvasError(f"Couldn't save a canvas read: {e}") from e
    note = f"diffdata saved this result: one canvas, {len(text)} characters of text."
    if "overflow_file" in record:
        note += (
            " It was too big to show you, so diffdata read Claude Code's saved copy. Don't "
            "read that file yourself."
        )
    return note


def sizes_dir(data: Path) -> Path:
    return data / "slack" / "canvas-sizes"


def search_rows(response) -> list[tuple[str, str]] | None:
    """(file ID, size) for each file a files search shows. None if any file doesn't show
    exactly one ID and one size, or the results aren't where diffdata expects them."""
    found = result_json(response)
    text = found.get("results") if found else None
    if not isinstance(text, str):
        return None
    blocks = FILE_BLOCK.split(text)[1:]
    count = FILE_COUNT.search(text)
    if (int(count[1]) if count else 0) != len(blocks):
        return None
    rows: dict[str, str] = {}
    for block in blocks:
        head = FILE_CONTENT.split(block, maxsplit=1)[0]
        ids, sizes = FILE_ID.findall(head), FILE_SIZE.findall(head)
        if len(ids) != 1 or len(sizes) != 1:
            return None
        file_id, size = ids[0].strip(), sizes[0].strip()
        if not ID_VALUE.fullmatch(file_id) or not SIZE_VALUE.fullmatch(size):
            return None
        if rows.setdefault(file_id, size) != size:  # one file shown twice, with two sizes
            return None
    return list(rows.items())


def save_search(
    data: Path,
    run_id: str,
    channel_id: str,
    canvases: list[str],
    response,
    call: str,
    saved_at: str,
) -> str:
    """Save the size of each listed canvas a files search shows. Every other row is dropped
    before anything is written, so no other file's ID, title or author reaches disk (CON-1).
    Returns the note for Claude, which gives counts only."""
    rows = search_rows(response)
    if rows is None:
        raise CanvasError(
            "This files search shows a file without an ID or size, or diffdata can't find its "
            "results, so nothing was saved. The connector's format may have changed. Stop the "
            "export."
        )
    sizes = {file_id: size for file_id, size in rows if file_id in canvases}
    record = {"saved_at": saved_at, "run_id": run_id, "channel_id": channel_id, "sizes": sizes}
    path = sizes_dir(data) / run_id / f"search_files-{call}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise CanvasError(f"Couldn't save a files search's canvas sizes: {e}") from e
    note = f"diffdata saved this result: {len(rows)} file(s), {len(sizes)} of them listed canvases."
    cursor = next_cursor(response)
    return note + (
        f" Next page: call again with cursor `{cursor}`." if cursor else " No more pages."
    )


def search_sizes(data: Path, run_id: str) -> dict[str, str]:
    """Each listed canvas's size, from the files searches saved in one run."""
    sizes: dict[str, str] = {}
    for path in sorted((sizes_dir(data) / run_id).glob("*.json")):
        try:
            found = json.loads(path.read_text(encoding="utf-8"))["sizes"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            continue  # a canvas without a size is read, so skipping is the safe way to fail
        if isinstance(found, dict):
            sizes.update(found)
    return sizes


def last_read(data: Path, canvas_id: str) -> tuple[datetime, str] | None:
    """When a canvas was last read, and in which run, from its newest saved read. Its raw
    folders are named by run ID, which sorts in time order."""
    raw = document_dir(data, SOURCE, canvas_id) / "raw"
    for folder in sorted((p for p in raw.glob("*") if p.is_dir()), reverse=True):
        for path in sorted(folder.glob("*.json")):
            try:
                return exact(json.loads(path.read_text(encoding="utf-8"))["saved_at"]), folder.name
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                DocumentError,
            ):
                continue
    return None


def tab_notices(messages: Iterable[dict]) -> dict[str, datetime]:
    """Canvas ID to the time of the newest Slackbot canvas-tab notice naming it. Messages are
    the Slack store's records: user_id, text and ts."""
    newest: dict[str, datetime] = {}
    for message in messages:
        if message.get("user_id") != SLACKBOT:
            continue
        when = datetime.fromtimestamp(int(message["ts"].split(".")[0]), UTC)
        for canvas in TAB_NOTICE.findall(message.get("text") or ""):
            newest[canvas] = max(when, newest.get(canvas, when))
    return newest


def reasons(
    last: datetime | None,
    size_then: str | None,
    size_now: str | None,
    notice: datetime | None,
    refresh_days: int,
    now: datetime,
) -> list[str]:
    """Why a canvas should be read this run; an empty list means it can be skipped.
    last: its last read. size_then: its size in the files search of that read's run.
    size_now: in this run's search. notice: the newest canvas-tab notice naming it."""
    if last is None:
        return [NEVER_READ]
    why = []
    if size_now is None:
        why.append(MISSING)
    elif size_then is None:
        why.append(NO_OLD_SIZE)
    elif size_now != size_then:
        why.append(SIZE_CHANGED)
    if notice is not None and notice > last:
        why.append(NOTICE)
    if now - last >= timedelta(days=refresh_days):
        why.append(REFRESH)
    return why


def due(
    data: Path,
    run_id: str,
    canvases: list[str],
    notices: dict[str, datetime],
    refresh_days: int,
    now: datetime,
) -> dict[str, list[str]]:
    """Each listed canvas, with the reasons to read it in this run (empty: skip it)."""
    sizes_now = search_sizes(data, run_id)
    found = {}
    for canvas in canvases:
        last = last_read(data, canvas)
        size_then = search_sizes(data, last[1]).get(canvas) if last else None
        found[canvas] = reasons(
            last[0] if last else None,
            size_then,
            sizes_now.get(canvas),
            notices.get(canvas),
            refresh_days,
            now,
        )
    return found


def read_versions(folder: Path) -> tuple[list[Version], int]:
    """A version for every saved read of one canvas, oldest first, and how many saved
    reads couldn't be read."""
    versions, unreadable = [], 0
    for path in folder.glob("raw/*/*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            text = result_json(record["tool_response"])["markdown_content"]
            if not isinstance(text, str):
                raise TypeError
            exact(record["saved_at"])  # refuses a time it can't read
            raw = path.relative_to(folder).as_posix()
            versions.append(Version(text=text, seen_at=record["saved_at"], raw=raw))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            DocumentError,
        ):
            unreadable += 1
    return sorted(versions, key=lambda v: (exact(v.seen_at), v.raw)), unreadable


def title(text: str, canvas_id: str) -> str:
    """The canvas's first heading, else its ID. A read gives no title of its own."""
    heading = HEADING.search(text)
    return heading[1] if heading else canvas_id


def show_names(text: str, people: dict[str, str], channels: dict[str, str]) -> str:
    """<@U...> becomes @Name and <#C...> becomes #name. An unknown ID stays an ID."""

    def replace(match: re.Match) -> str:
        sigil, target, label = match.groups()
        names = people if sigil == "@" else channels
        return sigil + (label or names.get(target) or target)

    return MENTION.sub(replace, text)


def build(data: Path, workspace: str, people: dict[str, str], channels: dict[str, str]) -> Counter:
    """Write every canvas that has saved reads. people and channels map IDs to names, for
    the clean copy. Returns counts only."""
    counts: Counter = Counter()
    folders = sorted(p for p in (documents_dir(data) / SOURCE).glob("*") if p.is_dir())
    for folder in folders:
        versions, unreadable = read_versions(folder)
        if unreadable:
            # Building from only part of raw would drop good versions and could show older
            # text, so a canvas with an unreadable read is left as it was.
            counts.update(unreadable=unreadable, skipped=1)
            continue
        if not versions:
            continue
        canvas_id = folder.name
        # The latest text as the shared format orders it, so the title matches the clean copy.
        _, latest = keep_changes(versions)[-1]
        document = Document(
            source=SOURCE,
            id=canvas_id,
            title=title(latest, canvas_id),
            tags={"workspace": workspace},
        )
        try:
            written = write_document(
                data, document, versions, names=lambda text: show_names(text, people, channels)
            )
        except DocumentError:
            counts["failed"] += 1
            continue
        counts.update(
            canvases=1,
            reads=len(versions),
            versions=written["versions"],
            written=written["written"],
        )
    return counts
