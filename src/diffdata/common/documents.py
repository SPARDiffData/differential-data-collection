"""The shared document format: how every source stores a document (ORG-1, ORG-2, COL-9, COL-10).

A document is text that changes over time, such as a Slack canvas or a Google Doc. Each
source fetches in its own way, but every source stores documents the same way. To add one:
  1. Save each fetched result unchanged with `save_raw()`.
  2. Write a reader that turns a document's raw files into `Version`s and `Event`s: one
     Version per fetch of the text (or per native revision), and every comment or suggestion
     each fetch shows. Repeats are fine: `write_document()` keeps only what's new.
  3. Pass them to `write_document()`, which writes everything else.

Layout under the data folder:
  documents/current/<source>_<title>_<id>.md   latest version of every document: a short
                                               header, then the text with names shown
  documents/<source>/<id>/
    raw/<fetch id>/<name>       every fetch, exactly as received: the only original
    versions/<time>-<hash>.md   one file per distinct text, exactly as fetched
    events.jsonl                edits, comments and suggestions: one JSON object per line,
                                in time order
    comments.md                 the comment threads, readable; only if there are comments
    meta.json                   the document's tags (COL-10)

Rules:
- Raw is the only original, and a saved raw file is never changed. Everything else is
  rebuilt from raw on every build and written only when it changed, so deleting everything
  but raw/ and building again gives the same files.
- A version is kept only when its text differs from the version before it, so going back
  to older text is a new version (COL-9). Versions are full texts, not diffs.
- Comments and suggestions live in events.jsonl, never in a version or the clean copy.
- No emails. An actor is the source's ID for someone and their display name. If a source
  shows only an email, give the ID alone. Raw keeps what the source sent until scrubbing
  (SCR-2).
- Times are UTC, written like 2026-10-13T14:00:00Z.
- Sources, IDs and fetch IDs are letters, digits, - and _ only (raw file names may add
  dots), so nothing can land outside the document's folder.

Every event has `type`, `event_id` (stable, so a rebuild gives the same file), `at` (when
it happened, if the source says), `seen_at` (when we first collected it), `actor` ({id,
name}, or null) and `raw` (the raw file it came from, like raw/<fetch id>/<name>). By type:
  version      An edit that made a new version. Adds `version` (its file name), `origin`
               ("snapshot" for our own copy, "native" for the source's own history, such
               as a Google revision) and `native_id`. A snapshot also has `not_before`:
               the last collection that still showed the old text, so the edit happened
               between not_before and seen_at.
  comment, reply, resolve, reopen
               Add `text`, `parent` (the native ID of the comment a reply, resolve or
               reopen belongs to), `anchor` ({quote, section}: what it points at),
               `version` (the version current at `at`, if known; for snapshots, the last
               one seen by then) and `native_id`.
  suggestion   Adds `kind` (insert or delete), `text`, `anchor` and `native_id`.
Anything reading events.jsonl skips types it doesn't know, so adding a type breaks nothing.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from diffdata import __version__

# Sources, IDs and fetch IDs become folder names. Raw file names may also have dots.
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")
SAFE_FILE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
COMMENT_TYPES = ("comment", "reply", "resolve", "reopen")
# What each event type adds to the fields every event has. Version events come from `Version`.
EVENT_FIELDS = {
    **dict.fromkeys(COMMENT_TYPES, ("text", "parent", "anchor", "version", "native_id")),
    "suggestion": ("kind", "text", "anchor", "native_id"),
}
# How each comment event starts in comments.md.
COMMENT_HEADS = {
    "comment": "**{who}** · {when}",
    "reply": "**{who}** · {when} · reply",
    "resolve": "Resolved by {who} · {when}",
    "reopen": "Reopened by {who} · {when}",
}


class DocumentError(Exception):
    """A document can't be stored as asked. The message says why, in plain words."""


def no_email(*values: str | None) -> None:
    if any(EMAIL.search(value) for value in values if value):
        raise DocumentError(
            "An actor or tag holds an email address, and the document format keeps none. "
            "Use the source's ID and display name."
        )


@dataclass(frozen=True)
class Actor:
    """Who did something: the source's ID for them and their display name. Never an email."""

    id: str | None = None
    name: str | None = None

    def __post_init__(self):
        no_email(self.id, self.name)


@dataclass(frozen=True)
class Anchor:
    """What a comment or suggestion points at: the quoted text, and its section if known."""

    quote: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class Document:
    source: str  # the kind of source, like "slack-canvas" or "google-doc"
    id: str  # the source's own ID for the document
    title: str
    link: str | None = None
    created_at: str | None = None
    tags: dict[str, str] = field(default_factory=dict)  # more tags for meta.json

    def __post_init__(self):
        no_email(*self.tags.values())


@dataclass(frozen=True)
class Version:
    """The text as one fetch saw it, or one revision from the source's own history."""

    text: str
    seen_at: str  # when we collected it
    raw: str  # the raw file it came from, like "raw/<fetch id>/<name>"
    made_at: str | None = None  # when the source says it was made
    actor: Actor | None = None
    native_id: str | None = None  # the source's revision ID; None for our own snapshot


@dataclass(frozen=True)
class Event:
    """A comment, reply, resolve, reopen or suggestion, as one fetch saw it."""

    type: str
    seen_at: str
    raw: str
    at: str | None = None
    actor: Actor | None = None
    native_id: str | None = None
    text: str | None = None
    parent: str | None = None  # the native ID of the comment it belongs to
    anchor: Anchor | None = None
    kind: str | None = None  # a suggestion's kind: insert or delete


def documents_dir(data: Path) -> Path:
    return data / "documents"


def safe(value: str, pattern: re.Pattern = SAFE_NAME) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DocumentError(f"{value!r} can't name a folder: use letters, digits, - and _ only.")
    return value


def document_dir(data: Path, source: str, doc_id: str) -> Path:
    """A document's folder. A source or ID that could point anywhere else is refused."""
    return documents_dir(data) / safe(source) / safe(doc_id)


def save_raw(
    data: Path, source: str, doc_id: str, fetch_id: str, name: str, content: str | bytes
) -> Path:
    """Save one fetched result unchanged, as raw/<fetch id>/<name> in the document's folder.
    Saving the same file again does nothing; other content under its name is refused."""
    path = document_dir(data, source, doc_id) / "raw" / safe(fetch_id) / safe(name, SAFE_FILE)
    body = content.encode("utf-8") if isinstance(content, str) else content
    if path.is_file():
        if path.read_bytes() != body:
            raise DocumentError(
                f"raw/{fetch_id}/{name} is already saved with other content, and raw files "
                "are never changed."
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def write_document(
    data: Path,
    document: Document,
    versions: Iterable[Version],
    events: Iterable[Event] = (),
    names: Callable[[str], str] | None = None,
) -> dict[str, int]:
    """Write a document's versions, events.jsonl, comments.md, meta.json and clean copy from
    what its reader found in raw. Each file is written only when it changed, and files this
    build no longer makes are removed. `names` turns text into what a person reads, such as
    mentions shown as names. Only the clean copy and comments.md use it, so versions keep
    the text as fetched. Returns counts only."""
    folder = document_dir(data, document.source, document.id)
    versions, events = list(versions), list(events)
    if not versions:
        raise DocumentError("A document needs at least one version of its text.")
    show = names or (lambda text: text)
    kept = keep_changes(versions)
    rows = sorted(
        [event for event, _ in kept] + comment_rows(events, kept),
        key=lambda row: (row["at"] or row["seen_at"], row["seen_at"], row["event_id"]),
    )
    latest, text = kept[-1]
    changed_at = latest["at"] or latest["seen_at"]
    collected_at = max(utc(seen.seen_at) for seen in [*versions, *events])
    meta = {
        "source": document.source,
        "id": document.id,
        "title": document.title,
        "link": document.link,
        "created_at": utc(document.created_at),
        "last_changed": changed_at,
        "last_collected": collected_at,
        "versions": len(kept),
        "events": len(rows),
        "tool": f"diffdata {__version__}",
    }
    if clash := meta.keys() & document.tags.keys():
        raise DocumentError(f"Tags can't replace the standard ones: {', '.join(sorted(clash))}.")

    files = {f"versions/{event['version']}": version_text for event, version_text in kept}
    files["events.jsonl"] = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    files["meta.json"] = json.dumps(meta | document.tags, indent=2, ensure_ascii=False) + "\n"
    if comments := render_comments(
        document.title, [row for row in rows if row["type"] in COMMENT_TYPES], show
    ):
        files["comments.md"] = comments
    stale = [
        path
        for path in [*folder.glob("versions/*.md"), folder / "comments.md"]
        if path.is_file() and path.relative_to(folder).as_posix() not in files
    ]

    # The clean copy's name still makes sense when it's copied out of its folder.
    slug = re.sub(r"[^a-z0-9]+", "-", document.title.lower()).strip("-")[:60].strip("-")
    slug = slug or "untitled"
    current = documents_dir(data) / "current" / f"{document.source}_{slug}_{document.id}.md"
    older = re.compile(rf"{re.escape(document.source)}_[a-z0-9-]+_{re.escape(document.id)}\.md")
    stale += [p for p in current.parent.glob("*.md") if p != current and older.fullmatch(p.name)]
    header = [
        "---",
        f"title: {json.dumps(document.title, ensure_ascii=False)}",
        f"source: {document.source}",
        f"id: {document.id}",
        *([f"link: {document.link}"] if document.link else []),
        f"last_changed: {changed_at}",
        f"collected_at: {collected_at}",
        f"tool: diffdata {__version__}",
        "---",
        "",
        "",
    ]

    written = sum(write_if_changed(folder / name, body) for name, body in files.items())
    written += write_if_changed(current, "\n".join(header) + show(text))
    for path in stale:
        path.unlink()
    return {"versions": len(kept), "events": len(rows), "written": written, "removed": len(stale)}


def keep_changes(versions: list[Version]) -> list[tuple[dict, str]]:
    """Each version whose text differs from the one before, oldest first, as its version
    event and its text."""
    kept: list[tuple[dict, str]] = []
    before = None
    for v in sorted(versions, key=lambda v: (utc(v.made_at or v.seen_at), utc(v.seen_at))):
        if before is None or v.text != before.text:
            at, seen_at = utc(v.made_at), utc(v.seen_at)
            name = f"{(at or seen_at).replace('-', '').replace(':', '')}-{digest(v.text)}.md"
            event = {
                "type": "version",
                "event_id": f"version-{name.removesuffix('.md')}",
                "at": at,
                "seen_at": seen_at,
                "actor": asdict(v.actor) if v.actor else None,
                "raw": v.raw,
                "version": name,
                "origin": "snapshot" if v.native_id is None else "native",
                "native_id": v.native_id,
            }
            if v.native_id is None:
                event["not_before"] = utc(before.seen_at) if before else None
            kept.append((event, v.text))
        before = v
    return kept


def comment_rows(events: list[Event], kept: list[tuple[dict, str]]) -> list[dict]:
    """The event log's rows for comments and suggestions: each one once, as first seen."""
    rows: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: utc(e.seen_at)):
        if e.type not in EVENT_FIELDS:
            raise DocumentError(f"{e.type!r} isn't an event type: use {', '.join(EVENT_FIELDS)}.")
        at, actor = utc(e.at), asdict(e.actor) if e.actor else None
        fields = {
            "text": e.text,
            "parent": e.parent,
            "anchor": asdict(e.anchor) if e.anchor else None,
            "kind": e.kind,
            "native_id": e.native_id,
        }
        # Every fetch shows the same comment again. Its native ID says it's the same one.
        same = [e.type, e.native_id] if e.native_id else [e.type, at, actor, fields]
        event_id = f"{e.type}-{digest(json.dumps(same, sort_keys=True))}"
        if event_id in rows:
            continue
        before = [v["version"] for v, _ in kept if at and (v["at"] or v["seen_at"]) <= at]
        fields["version"] = before[-1] if before else None
        row = {"type": e.type, "event_id": event_id, "at": at, "seen_at": utc(e.seen_at)}
        row |= {"actor": actor, "raw": e.raw}
        rows[event_id] = row | {name: fields[name] for name in EVENT_FIELDS[e.type]}
    return list(rows.values())


def render_comments(title: str, rows: list[dict], show: Callable[[str], str]) -> str:
    """comments.md: each thread under the text it points at, oldest first. Empty if none."""
    threads: dict[str, list[dict]] = {}
    for row in rows:
        threads.setdefault(row["parent"] or row["native_id"] or row["event_id"], []).append(row)
    lines = [f"# Comments: {title}"] if threads else []
    for number, thread in enumerate(threads.values(), start=1):
        lines += ["", f"## Thread {number}", ""]
        if thread[0]["type"] != "comment":
            lines += ["_The comment this thread belongs to wasn't collected._", ""]
        if quote := (thread[0]["anchor"] or {}).get("quote"):
            lines += [f"> {line}" if line else ">" for line in show(quote).splitlines()] + [""]
        for row in thread:
            actor = row["actor"] or {}
            who = actor.get("name") or actor.get("id") or "Someone"
            when = stamp(row["at"]) if row["at"] else f"seen {stamp(row['seen_at'])}"
            lines.append(COMMENT_HEADS[row["type"]].format(who=who, when=when))
            lines += show(row["text"]).splitlines() if row["text"] else []
            lines.append("")
    return "\n".join(lines)


def write_if_changed(path: Path, text: str) -> bool:
    """Write `text` exactly, with no newline changes, unless the file already holds it."""
    body = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return True


def utc(value: str | None) -> str | None:
    """A time in UTC, like 2026-10-13T14:00:00Z. A time with no time zone is refused."""
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise DocumentError(f"{value!r} isn't a time.") from e
    if when.tzinfo is None:
        raise DocumentError(f"The time {value!r} has no time zone, so it can't be put in UTC.")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(when: str) -> str:
    return f"{when[:10]} {when[11:16]} UTC"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
