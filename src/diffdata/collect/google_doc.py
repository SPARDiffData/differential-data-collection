"""Google Docs in the shared document format (COL-4, COL-5, COL-9): the reader.

This file doesn't fetch anything. However a doc is fetched (the producer's own Google
login, a doc shared with a project account, the Drive connector in Claude, or a changelog
saved by hand from the browser), the fetcher saves what it got with `save_fetch()`. Then
`build()` reads every fetch it finds and writes the doc in the shared format
(common/documents.py). So a new way of fetching only has to save these files:

  documents/google-doc/<doc id>/raw/<fetch id>/
    fetch.json            needed: {"fetched_at": "<time with a time zone>", "route": "..."}
    metadata.json         Drive's files.get: name, createdTime, modifiedTime
    export.txt            the current text, as Drive exports it (text/plain)
    comments.json         Drive's comments.list: comments with their replies
    revisions.json        Drive's revisions.list: when each revision was made, and by whom
    revision-<id>.txt     the text of one Drive revision, exported
    changelog.json        the Docs changelog, as Google sends it (the )]}' prefix is fine)

Every file but fetch.json can be left out, and a fetch can save other files too (they
stay in raw, unread). comments.json and revisions.json can be Drive's page exactly as
sent ({"comments": [...], "nextPageToken": ...}) or just the list. A list Drive sends in
several pages is saved one page a file: comments.json, then comments-2.json and so on.
What the reader makes of them:
- Versions, best first. The newest changelog gives the full history: it's replayed, and
  each stretch of editing becomes a version (a new one when the editor or the editing
  session changes, or after IDLE without edits). Else each revision-<id>.txt is a
  version. With a changelog, a revision made after the newest changelog was fetched is
  kept too. An export.txt is our own snapshot (COL-9). One fetched no later than the
  newest changelog is dropped, since the changelog already covers it, and so is one whose
  text matches the version before it apart from line endings.
- Comments, replies, resolves and reopens, from comments.json. Every fetch shows them
  again; the shared format keeps each one once.
- Suggestions, from the changelog: one per suggestion ID, with the text it inserts or
  marks for deletion, after any typing fixed or marks taken back within it.
The changelog's single edits (every insert and delete) stay in raw for now: the shared
format has no event type for them yet.
"""

import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from diffdata.collect.gdocs_changelog import DocState, edit_events, iso
from diffdata.common.documents import (
    Actor,
    Anchor,
    Document,
    DocumentError,
    Event,
    Version,
    documents_dir,
    exact,
    save_raw,
    write_document,
)

SOURCE = "google-doc"
DOC_URL = "https://docs.google.com/document/d/{id}"
ROUTES = ("google-login", "shared-account", "connector", "browser-file")
# A stretch of editing ends when nobody has edited for this long.
IDLE = timedelta(minutes=30)
# Google puts this in front of the changelog's JSON so a browser won't run it as a script.
XSSI_PREFIX = ")]}'"
REPLY_TYPES = {"resolve": "resolve", "reopen": "reopen"}
# One page of a Drive list: comments.json, comments-2.json, revisions.json and so on.
PAGE = re.compile(r"(comments|revisions)(?:-(\d+))?\.json")


class GoogleDocError(Exception):
    """A Google Doc fetch can't be saved or read. The message says why, in plain words."""


@dataclass
class Fetch:
    """One fetch's files, as saved in raw."""

    folder: Path  # raw/<fetch id>
    fetched_at: str
    route: str
    files: dict[str, Path] = field(default_factory=dict)

    def raw(self, name: str) -> str:
        return f"raw/{self.folder.name}/{name}"

    def json(self, name: str):
        if name not in self.files:
            return None
        text = self.files[name].read_text(encoding="utf-8-sig").lstrip()
        if name == "changelog.json" and text.startswith(XSSI_PREFIX):
            text = text[len(XSSI_PREFIX) :]
        return json.loads(text)

    def text(self, name: str) -> str | None:
        if name not in self.files:
            return None
        return self.files[name].read_text(encoding="utf-8-sig")

    def pages(self, stem: str) -> list[tuple[str, dict]]:
        """Every item of a Drive list saved as <stem>.json, <stem>-2.json and so on, with
        the raw file it came from. Each file is Drive's page as sent, or just its list."""
        names = [n for n in self.files if (m := PAGE.fullmatch(n)) and m[1] == stem]
        found = []
        for name in sorted(names, key=lambda n: int(PAGE.fullmatch(n)[2] or 1)):
            page = self.json(name)
            items = page.get(stem, []) if isinstance(page, dict) else page
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise GoogleDocError(f"{self.raw(name)} isn't a list of {stem}.")
            found += [(self.raw(name), item) for item in items]
        return found


def save_fetch(
    data: Path, doc_id: str, fetch_id: str, fetched_at: str, route: str, files: dict[str, str]
) -> Path:
    """Save one fetch of a doc unchanged, with fetch.json saying when and how. `files` maps
    a raw file name to what was fetched, exactly as received. Returns the fetch's folder."""
    if route not in ROUTES:
        raise GoogleDocError(f"{route!r} isn't a route: use {', '.join(ROUTES)}.")
    try:
        exact(fetched_at)  # refuses a time with no time zone
    except DocumentError as e:
        raise GoogleDocError(f"fetched_at can't be used: {e}") from e
    if "fetch.json" in files:
        raise GoogleDocError("fetch.json is written by save_fetch, from fetched_at and route.")
    manifest = json.dumps({"fetched_at": fetched_at, "route": route}, indent=2) + "\n"
    try:
        # fetch.json goes last: until it's there, the reader sees the fetch as unfinished,
        # so a save cut short is never built from.
        for name, content in {**files, "fetch.json": manifest}.items():
            path = save_raw(data, SOURCE, doc_id, fetch_id, name, content)
    except DocumentError as e:
        raise GoogleDocError(f"Couldn't save a Google Doc fetch: {e}") from e
    return path.parent


def read_fetches(folder: Path) -> tuple[list[Fetch], int, int]:
    """Every finished fetch of one doc, oldest first; how many couldn't be read; and how
    many are unfinished (no fetch.json yet, as a save cut short leaves them)."""
    fetches, unreadable, unfinished = [], 0, 0
    for path in folder.glob("raw/*/"):
        if not (path / "fetch.json").exists():
            unfinished += 1  # saving it again with the same fetch ID finishes it
            continue
        try:
            manifest = json.loads((path / "fetch.json").read_text(encoding="utf-8"))
            exact(manifest["fetched_at"])
            # A name starting with . is a save still in progress (see save_raw).
            files = {
                p.name: p
                for p in path.iterdir()
                if p.is_file() and p.name != "fetch.json" and not p.name.startswith(".")
            }
            fetches.append(Fetch(path, manifest["fetched_at"], manifest.get("route"), files))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            DocumentError,
        ):
            unreadable += 1
    fetches.sort(key=lambda f: (exact(f.fetched_at), f.folder.name))
    return fetches, unreadable, unfinished


def drive_actor(user: dict | None) -> Actor | None:
    """A Drive user as an actor: their permission ID and display name, never an email."""
    if not user:
        return None
    perm = f"perm:{user['permissionId']}" if user.get("permissionId") else None
    try:
        return Actor(perm, user.get("displayName"))
    except DocumentError:
        # The display name is an email, as it is for someone without a Google account.
        return Actor(perm) if perm else None


def comment_events(fetch: Fetch) -> list[Event]:
    events = []
    for raw, comment in fetch.pages("comments"):
        quote = (comment.get("quotedFileContent") or {}).get("value")
        events.append(
            Event(
                "comment",
                fetch.fetched_at,
                raw,
                at=comment.get("createdTime"),
                actor=drive_actor(comment.get("author")),
                native_id=comment["id"],
                text=comment.get("content"),
                anchor=Anchor(html.unescape(quote)) if quote else None,
            )
        )
        for reply in comment.get("replies") or []:
            events.append(
                Event(
                    REPLY_TYPES.get(reply.get("action"), "reply"),
                    fetch.fetched_at,
                    raw,
                    at=reply.get("createdTime"),
                    actor=drive_actor(reply.get("author")),
                    native_id=reply["id"],
                    text=reply.get("content") or None,
                    parent=comment["id"],
                )
            )
    return events


def changelog_history(fetches: list[Fetch]) -> tuple[list[Version], list[Event], int]:
    """Replay the newest changelog among `fetches` (oldest first; each holds the whole
    history up to when it was fetched). Returns a version per stretch of editing, a
    suggestion per suggestion ID, and how many warnings the replay gave."""
    newest = fetches[-1]
    state, versions, suggestions = DocState(), [], {}
    stretch = None  # {session, at (its last edit), editor, rev}

    def first_seen(at: str | None) -> tuple[str, str]:
        """When we first collected something made at `at`, and the raw file it was in:
        the first changelog fetched after it. So fetching again changes nothing old."""
        made = exact(at) if at else None
        fetch = next((f for f in fetches if made and exact(f.fetched_at) >= made), newest)
        return fetch.fetched_at, fetch.raw("changelog.json")

    def close() -> None:
        text = state.base_text()
        if not versions and not text:
            return  # the empty doc before anyone typed isn't a version
        actor = Actor(stretch["editor"]) if stretch["editor"] else None
        rev = f"rev-{stretch['rev']}"
        versions.append(Version(text, *first_seen(stretch["at"]), stretch["at"], actor, rev))

    for edit in edit_events(newest.json("changelog.json")):
        at = iso(edit["ts_ms"])
        if edit["source"] == "snapshot" and stretch is None:
            # The doc as it stood when the changelog starts: a version of its own, by nobody
            # we know, even if no edits follow.
            stretch = {"session": None, "at": at, "editor": None, "rev": edit["rev"]}
        elif edit["source"] == "changelog":
            # Google leaves the session out of some entries, so a missing one isn't a new
            # session. A new editor, a new session or a pause starts a new stretch.
            session = edit["session"] or (stretch or {}).get("session")
            if stretch is not None and (
                edit["actor"] != stretch["editor"]
                or session != stretch["session"]
                or exact(at) - exact(stretch["at"]) > IDLE
            ):
                close()
            stretch = {"session": session, "at": at, "editor": edit["actor"], "rev": edit["rev"]}
        state.apply(edit)
        kind = {"suggest_insert": "insert", "suggest_delete_mark": "delete"}.get(edit["kind"])
        if kind:
            sid = edit.get("suggestion_id")
            native_id = sid or f"{kind}-rev{edit['rev']}-{edit['op_in_rev']}"
            found = suggestions.setdefault(
                native_id, {"kind": kind, "at": at, "actor": edit["actor"], "sid": sid, "text": ""}
            )
            # Its text so far, with any typing fixed or marks taken back since.
            now = state.suggestion_text(sid) if sid else edit.get("text") or edit.get("marked_text")
            found["text"] = now or found["text"]
    if stretch is not None:
        close()
    events = []
    for native_id, found in suggestions.items():
        # Still in the doc at the end, or as it last stood before it was accepted or rejected.
        text = (state.suggestion_text(found["sid"]) if found["sid"] else "") or found["text"]
        actor = Actor(found["actor"]) if found["actor"] else None
        events.append(
            Event(
                "suggestion",
                *first_seen(found["at"]),
                at=found["at"],
                actor=actor,
                native_id=native_id,
                text=text,
                kind=found["kind"],
            )
        )
    return versions, events, len(state.warnings)


def revision_versions(fetch: Fetch, revisions: dict[str, dict]) -> list[Version]:
    """A native version for each revision-<id>.txt in one fetch."""
    versions = []
    for name in sorted(fetch.files):
        if name.startswith("revision-") and name.endswith(".txt"):
            native_id = name.removeprefix("revision-").removesuffix(".txt")
            revision = revisions.get(native_id, {})
            versions.append(
                Version(
                    fetch.text(name),
                    fetch.fetched_at,
                    fetch.raw(name),
                    revision.get("modifiedTime"),
                    drive_actor(revision.get("lastModifyingUser")),
                    native_id,
                )
            )
    return versions


def same_text(a: str, b: str) -> bool:
    """The same text, apart from line endings and whitespace at the end."""
    return a.replace("\r\n", "\n").rstrip() == b.replace("\r\n", "\n").rstrip()


def reconcile(native: list[Version], snapshots: list[Version], covered_to: str | None):
    """The snapshots worth keeping beside the native versions (COL-9): those the changelog
    doesn't cover, whose text differs from the latest version before them."""
    kept = []
    for snapshot in snapshots:
        seen = exact(snapshot.seen_at)
        if covered_to and seen <= exact(covered_to):
            continue
        before = [v for v in native + kept if exact(v.made_at or v.seen_at) <= seen]
        latest = max(before, key=lambda v: exact(v.made_at or v.seen_at), default=None)
        if latest is None or not same_text(latest.text, snapshot.text):
            kept.append(snapshot)
    return kept


@dataclass
class Reading:
    """What the reader found in one doc's raw files."""

    document: Document
    versions: list[Version]
    events: list[Event]
    history: str  # "changelog", "revisions" or "snapshots"
    # Edits the replay couldn't place exactly (an index out of range and so on). More than
    # none means the changelog's format may have changed: check the versions by eye.
    replay_warnings: int = 0


def read(folder: Path, fetches: list[Fetch]) -> Reading | None:
    """Turn one doc's fetches into the shared format's versions and events."""
    doc_id = folder.name
    events = [event for fetch in fetches for event in comment_events(fetch)]
    revisions = {}
    for fetch in fetches:
        revisions |= {str(r.get("id")): r for _, r in fetch.pages("revisions")}

    # The changelog always holds the whole history, so the newest one has everything.
    with_changelog = [f for f in fetches if "changelog.json" in f.files]
    from_revisions = [v for fetch in fetches for v in revision_versions(fetch, revisions)]
    native, covered_to, warnings = [], None, 0
    if with_changelog:
        native, suggestions, warnings = changelog_history(with_changelog)
        events += suggestions
    if native:
        # The changelog covers the doc up to when it was fetched, so an export or a Drive
        # revision from then or before adds nothing. (Drive's text export also lays out lists
        # and paragraphs differently from the replay, so the two texts never match exactly.)
        # One made later is newer history, as when a doc changes fetch route.
        covered_to = with_changelog[-1].fetched_at
        native += [v for v in from_revisions if exact(v.made_at or v.seen_at) > exact(covered_to)]
        history = "changelog"
    else:
        # No changelog, or one whose replay gave no text: fall back to what else was saved.
        native = from_revisions
        history = "revisions" if native else "snapshots"
    snapshots = [
        Version(f.text("export.txt"), f.fetched_at, f.raw("export.txt"))
        for f in fetches
        if "export.txt" in f.files
    ]
    versions = native + reconcile(native, snapshots, covered_to)
    if not versions:
        return None

    meta = next((m for f in reversed(fetches) if (m := f.json("metadata.json"))), {})
    document = Document(
        source=SOURCE,
        id=doc_id,
        title=meta.get("name") or doc_id,
        link=DOC_URL.format(id=doc_id),
        created_at=meta.get("createdTime"),
        tags={"history": history},
    )
    return Reading(document, versions, events, history, warnings)


def build(data: Path) -> Counter:
    """Write every Google Doc that has saved fetches. Returns counts only."""
    counts: Counter = Counter()
    folders = sorted(p for p in (documents_dir(data) / SOURCE).glob("*") if p.is_dir())
    for folder in folders:
        fetches, unreadable, unfinished = read_fetches(folder)
        counts["unfinished fetches"] += unfinished
        if unreadable:
            # Building from only part of raw could drop good versions, so a doc with an
            # unreadable fetch is left as it was.
            counts.update(unreadable=unreadable, skipped=1)
            continue
        if not fetches:
            continue
        try:
            reading = read(folder, fetches)
            if reading is None:
                counts["empty"] += 1
                continue
            written = write_document(data, reading.document, reading.versions, reading.events)
        except (
            DocumentError,
            GoogleDocError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            AttributeError,
            ValueError,
        ) as e:
            # A raw file in a shape we didn't expect fails this doc only, not the build.
            counts["failed"] += 1
            counts[f"failed: {type(e).__name__}"] += 1
            continue
        counts.update(
            docs=1,
            fetches=len(fetches),
            versions=written["versions"],
            events=written["events"],
            written=written["written"],
        )
        counts[f"history: {reading.history}"] += 1
        counts["replay warnings"] += reading.replay_warnings
    return counts
