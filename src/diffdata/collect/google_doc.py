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
    changelog.json        the Docs changelog, as Google sends it (the )]}' prefix is fine).
                          Always the whole changelog, from the first revision on: the
                          reader replays only the newest one, starting from an empty doc,
                          so a changelog of only the new revisions would lose the rest.

Every file but fetch.json can be left out, and a fetch can save other files too (they
stay in raw, unread). comments.json and revisions.json can be Drive's page exactly as
sent ({"comments": [...], "nextPageToken": ...}) or just the list. A list Drive sends in
several pages is saved one page a file: comments.json, then comments-2.json and so on.
What the reader makes of them:
- Versions, best first. The newest changelog gives the full history: it's replayed, and
  each stretch of editing becomes a version (see `new_stretch` for when one starts, and
  LARGE_DELETION for a cut before a big deletion). Else each revision-<id>.txt is a
  version. With a changelog, a revision made after the newest changelog was fetched is
  kept too. An export.txt is our own snapshot (COL-9). One fetched no later than the
  newest changelog is dropped, since the changelog already covers it, and so is one whose
  text matches the version before it apart from line endings.
- Comments, replies, resolves and reopens, from comments.json. Every fetch shows them
  again; the shared format keeps each one once. Drive writes an @-mention as the person's
  email ("+sam@example.com"), though they typed a name, so it becomes MENTION_SHOWN.
- Suggestions, from the changelog: one per suggestion ID, with the text it inserts or
  marks for deletion, after any typing fixed or marks taken back within it. A replacement
  (both, under one ID) has the new text, and the old text as its anchor. Each suggestion
  that ended gets an accept or reject event (see `outcome`).
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
    EMAIL,
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
# A stretch of editing ends when nobody has edited for this long,
IDLE = timedelta(minutes=30)
# or when it has gone on this long, so an idea isn't lost inside one long sitting,
MAX_STRETCH = timedelta(hours=2)
# or when someone starts editing who hasn't edited for this long. People writing together
# take turns every few seconds, and that's one stretch, not one per turn.
CO_WRITING = timedelta(minutes=5)
# Deleting this much plain text (UTF-16 units) closes the version before it, so text
# someone wrote and then replaced still shows up in a version.
LARGE_DELETION = 100
# Google puts this in front of the changelog's JSON so a browser won't run it as a script.
XSSI_PREFIX = ")]}'"
REPLY_TYPES = {"resolve": "resolve", "reopen": "reopen"}
# One page of a Drive list: comments.json, comments-2.json, revisions.json and so on.
PAGE = re.compile(r"(comments|revisions)(?:-(\d+))?\.json")
# An @-mention in a Drive comment's text, which Drive writes as the person's email.
MENTION = re.compile(rf"[+@]{EMAIL.pattern}")
MENTION_SHOWN = "@[mention]"


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


def without_mentions(text: str | None) -> str | None:
    """A comment's text with each @-mention's email swapped for MENTION_SHOWN."""
    return MENTION.sub(MENTION_SHOWN, text) if text else text


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
                text=without_mentions(comment.get("content")),
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
                    text=without_mentions(reply.get("content")) or None,
                    parent=comment["id"],
                )
            )
    return events


def new_stretch(stretch: dict, editor: str | None, at: str, their_last: str | None) -> bool:
    """Whether an edit at `at` starts a new stretch of editing: after a pause of IDLE, once
    the stretch has gone on for MAX_STRETCH, or when someone starts editing who isn't in it
    and hasn't edited for CO_WRITING. Editing sessions don't count: one person with the
    doc open in two windows has two sessions, and switching between them isn't news."""
    now = exact(at)
    if now - exact(stretch["at"]) > IDLE or now - exact(stretch["first"]) > MAX_STRETCH:
        return True
    joining = editor not in stretch["editors"]
    return joining and (their_last is None or now - exact(their_last) > CO_WRITING)


def big_deletion(state: DocState, edit: dict) -> bool:
    """Whether an edit deletes LARGE_DELETION or more of the doc's plain text. Suggested
    text doesn't count, so accepting a long suggestion isn't one."""
    if edit["kind"] != "delete":
        return False
    return state.plain_count(edit["start"], edit["end"]) >= LARGE_DELETION


def outcome(entry: list[dict], sid: str, author: str | None) -> tuple[str, str | None]:
    """How a suggestion whose last text went in one changelog entry ended: ("accept",
    None), ("reject", None), or ("reject", "withdrawn") when its own author took it back.
    Google has no op for this. Accepting an insertion deletes the suggested text and
    inserts it again as plain text in the same entry. Accepting a deletion deletes the
    marked text. Anything else that removes it (dss, usfd, or a plain delete) rejects it."""
    inserts = [(e["index"], e["text"]) for e in entry if e["kind"] == "insert"]
    for edit in entry:
        removed = edit.get("removed")
        if edit["kind"] != "delete" or not removed:
            continue
        again = (removed["start"], removed["text"]) in inserts
        if (sid in removed["ins"] and again) or (sid in removed["marks"] and not again):
            return "accept", None
    actor = entry[0]["actor"]
    return "reject", "withdrawn" if actor and actor == author else None


def changelog_history(fetches: list[Fetch]) -> tuple[list[Version], list[Event], int]:
    """Replay the newest changelog among `fetches` (oldest first; each holds the whole
    history up to when it was fetched). Returns a version per stretch of editing, a
    suggestion per suggestion ID with its accept or reject, and how many warnings the
    replay gave."""
    newest = fetches[-1]
    state, versions, suggestions, outcomes = DocState(), [], {}, {}
    stretch = None  # {first, at (its last edit), rev, editors (edits per editor)}
    last_edit: dict[str | None, str] = {}  # when each editor last edited
    entry: list[dict] = []  # the ops of the changelog entry being replayed
    # Each suggestion's text just before this entry first removed or unmarked any of it:
    # what an accept accepted, after any fixes made in earlier entries.
    before: dict[str, dict[str, str]] = {}

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
        # Several people writing together: the one who made the most edits.
        editor = next(iter(stretch["editors"].most_common(1)), (None, 0))[0]
        actor = Actor(editor) if editor else None
        rev = f"rev-{stretch['rev']}"
        versions.append(Version(text, *first_seen(stretch["at"]), stretch["at"], actor, rev))

    def touches(edit: dict) -> set[str]:
        """The suggestions one edit removed text from, or unmarked. An op that only names a
        suggestion (a style op on it, say) can't end it."""
        removed = edit.get("removed") or {}
        return {*removed.get("ins", {}), *removed.get("marks", {}), *edit.get("unmarked", ())}

    def judge() -> None:
        """Log the outcome of each suggestion whose last text went in this entry."""
        touched = set().union(*map(touches, entry)) & suggestions.keys()
        if not touched:
            return
        for sid in touched - state.live():
            outcomes[sid] = (iso(entry[0]["ts_ms"]), entry[0]["actor"], entry[0]["rev"])
            outcomes[sid] += outcome(entry, sid, suggestions[sid]["actor"])
            if outcomes[sid][3] == "accept" and sid in before:
                # Accepted as it stood, with any typing fixed or marks narrowed since.
                for part, text in before[sid].items():
                    suggestions[sid][part] = text or suggestions[sid][part]

    for edit in edit_events(newest.json("changelog.json")):
        at = iso(edit["ts_ms"])
        if edit["source"] == "snapshot" and stretch is None:
            # The doc as it stood when the changelog starts: a version of its own, by nobody
            # we know, even if no edits follow. Google doesn't say when the snapshot was
            # taken, so its time is the first edit's, a bound rather than an edit time.
            stretch = {"first": at, "at": at, "rev": edit["rev"], "editors": Counter()}
        elif edit["source"] == "changelog":
            if entry and (edit["rev"], edit["ts_ms"]) != (entry[0]["rev"], entry[0]["ts_ms"]):
                judge()
                entry, before = [], {}
            if stretch is not None and (
                new_stretch(stretch, edit["actor"], at, last_edit.get(edit["actor"]))
                or big_deletion(state, edit)
            ):
                close()
                stretch = None
            if stretch is None:
                stretch = {"first": at, "editors": Counter()}
            stretch.update(at=at, rev=edit["rev"])
            stretch["editors"][edit["actor"]] += 1
            last_edit[edit["actor"]] = at
            entry.append(edit)
        if edit["kind"] in ("delete", "remove_suggested", "suggest_delete_unmark"):
            for sid in state.ids_in(edit["start"], edit["end"]) & suggestions.keys():
                before.setdefault(
                    sid, {p: state.suggestion_text(sid, p) for p in ("insert", "delete")}
                )
        state.apply(edit)
        part = {"suggest_insert": "insert", "suggest_delete_mark": "delete"}.get(edit["kind"])
        if part:
            sid = edit.get("suggestion_id")
            native_id = sid or f"{part}-rev{edit['rev']}-{edit['op_in_rev']}"
            found = suggestions.setdefault(
                native_id,
                {"at": at, "actor": edit["actor"], "sid": sid, "insert": "", "delete": ""},
            )
            # Typing into a suggestion after it seemed to end (its last character deleted,
            # then typed again) means it didn't end.
            outcomes.pop(native_id, None)
            if sid:
                # Its text so far, with any typing fixed since. Only when it grows: while
                # it's being withdrawn or rejected a letter at a time, it only shrinks.
                found[part] = state.suggestion_text(sid, part) or found[part]
            else:
                found[part] = edit.get("text") or edit.get("marked_text") or found[part]
    if entry:
        judge()
    for sid in state.live() & suggestions.keys():
        # Still pending at the end: its text now, after any fixes or marks taken back.
        for part in ("insert", "delete"):
            suggestions[sid][part] = state.suggestion_text(sid, part)
    if stretch is not None:
        close()
    events = []
    for native_id, found in suggestions.items():
        # Still in the doc at the end, or as it last stood before it was accepted or rejected.
        kind = "replace" if found["insert"] and found["delete"] else None
        kind = kind or ("delete" if found["delete"] else "insert")
        actor = Actor(found["actor"]) if found["actor"] else None
        events.append(
            Event(
                "suggestion",
                *first_seen(found["at"]),
                at=found["at"],
                actor=actor,
                native_id=native_id,
                text=found["delete"] if kind == "delete" else found["insert"],
                anchor=Anchor(found["delete"]) if kind == "replace" else None,
                kind=kind,
            )
        )
    for sid, (at, actor, _rev, type_, kind) in outcomes.items():
        events.append(
            Event(
                type_,
                *first_seen(at),
                at=at,
                actor=Actor(actor) if actor else None,
                native_id=sid,
                parent=sid,
                kind=kind,
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
