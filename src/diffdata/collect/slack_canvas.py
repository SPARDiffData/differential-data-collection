"""Slack canvases (COL-2): each opted-in canvas's text, with a new version each time it changes.

/slack-export reads every canvas listed in config.toml with `slack_read_canvas`, after the
channels. The hook passes each read to `save_read()`, which saves it unchanged as the
canvas's raw file, but only if it shows the canvas that was asked for (CON-1). `build()`
turns every saved read into a version and writes the canvas in the shared document format
(common/documents.py), which keeps a new version only when the text changed (COL-9).

A read is JSON: `canvas_id`, `markdown_content` (the text), `section_id_mapping` and, when
there are unresolved comments, `comment_threads`. It has no title, edit time or author, so
the title is the latest text's first heading, and every version is our own snapshot. A read
is saved with its time to the microsecond, so two reads in the same second keep their
order. The section mapping and comment threads stay in raw for COL-3. Links to images,
Docs and other canvases stay links: nothing here fetches them.

Under the data folder:
  documents/slack-canvas/<canvas id>/raw/<run id>/slack_read_canvas-<call>.json
and the rest of the shared format, including the clean copy in documents/current/.
"""

import json
import re
from collections import Counter
from pathlib import Path

from diffdata.common.documents import (
    Document,
    DocumentError,
    Version,
    documents_dir,
    exact,
    keep_changes,
    save_raw,
    write_document,
)
from diffdata.discover.slack_parse import result_json

SOURCE = "slack-canvas"
TOOL = "slack_read_canvas"
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
# Canvases mention people as <@U...> and channels as <#C...>, with no name attached. Only
# these are changed: canvas text can hold other <...>, such as <br>, which stays as it is.
MENTION = re.compile(r"<([@#])([A-Z][A-Z0-9]+)(?:\|([^<>\n]*))?>")


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
