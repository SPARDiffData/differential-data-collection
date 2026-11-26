"""Google Docs in the scrub (#147): who wrote each comment, and the comments copy.

Each opted-in doc gets two scrubbed copies (scrub/run.py): its current version, from the
clean copy in documents/current/, and its comments, built here from the doc's events.jsonl
(common/documents.py). Versions and suggestions aren't scrubbed or sent yet.

Comment authors are Drive display names, never emails. The people list comes from Slack, so
each author is matched to it by their whole name, compared as names are compared (any case,
no accents):
  - one of exactly one person's names or nicknames: that person's [Person N], so someone in
    Slack and in a doc gets the same placeholder (SCR-2)
  - nobody's: the scrub adds a person with that name and no Slack IDs, so the next scrub
    matches them by name again, and their name is replaced everywhere
  - two or more people's: [name], flagged
An author is set from this match, never by searching the text, so a one-letter name or one in
not_names still becomes their placeholder. An author with no name is "Someone". A Google ID
(perm:..., gaia:...) never goes in a copy.

Comments, replies, resolves and reopens by someone who opted out on Slack (slack_opted_out,
with the team's list) are left out, by their whole name, since a Google account can't be
tied to a Slack ID. The copy says how many, never whose.

A document copy's header (`---` lines, as common/documents.py writes them) is read strictly by
`split()`: its title is the only free text, and is scrubbed on its own, as text rather than as
the JSON string it's written as, so an escaped quote can't hide a name. Every other line has a
fixed shape (a source, an ID, a Google Doc link, times, the tool), holds no name, and is kept
as written, so a person called "Doc" can't change it.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from diffdata.collect import google_doc
from diffdata.common.documents import COMMENT_TYPES, document_dir, render_comments
from diffdata.scrub.people import PLACEHOLDER, People, Person, nfc
from diffdata.scrub.text import SHARED, Flag, key

SOURCE = google_doc.SOURCE  # "google-doc": the current version's copy
COMMENTS = f"{SOURCE}-comments"  # its comments' copy
SOMEONE = "Someone"
TIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
# Every header line but the title, and the shape its value must have.
FIXED = {
    "source": re.compile(r"slack-canvas|google-doc|google-doc-comments"),
    "id": re.compile(r"[A-Za-z0-9_-]+"),
    "link": re.compile(r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]+"),
    "last_changed": re.compile(TIME),
    "collected_at": re.compile(TIME),
    "tool": re.compile(r"diffdata [0-9]+\.[0-9]+\.[0-9]+"),
}


class DocError(Exception):
    """A doc's comments can't be read. The message names no doc and quotes no text."""


def comment_rows(data: Path, doc_id: str) -> list[dict] | None:
    """The doc's comment, reply, resolve and reopen rows from events.jsonl, or None if it has
    no events.jsonl. Raises DocError if it can't be read as the shared format writes it."""
    path = document_dir(data, SOURCE, doc_id) / "events.jsonl"
    if not path.is_file():
        return None
    try:
        # Only \n ends a line, as the shared format writes it: a comment can hold U+2028.
        text = path.read_bytes().decode("utf-8")
        rows = [json.loads(line) for line in text.split("\n") if line.strip()]
    except (OSError, UnicodeDecodeError, ValueError):
        raise DocError("its events.jsonl can't be read") from None
    found = []
    for row in rows:
        if not isinstance(row, dict):
            raise DocError("its events.jsonl isn't in the shared format")
        if row.get("type") not in COMMENT_TYPES:
            continue
        actor = row.get("actor")
        if actor is not None and not (
            isinstance(actor, dict) and isinstance(actor.get("name"), str | None)
        ):
            raise DocError("its events.jsonl isn't in the shared format")
        found.append(row)
    return found


def author_name(row: dict) -> str | None:
    """A row's author's display name, or None if it has none."""
    name = (row.get("actor") or {}).get("name")
    return nfc(name.strip()) if isinstance(name, str) and name.strip() else None


def has_letter(name: str) -> bool:
    return any(c.isalpha() for c in key(name))


@dataclass(frozen=True)
class Header:
    lines: tuple[tuple[str, str], ...]  # each line's name and value, the title's as text
    body: str  # everything after the header, as written


def split(text: str) -> Header | None:
    """A document copy's header and body, or None if the header isn't exactly one the shared
    format or `comments_copy` writes: each line once, a fixed-shape value, a title that's one
    JSON string."""
    if not text.startswith("---\n"):
        return None
    head, end, body = text[4:].partition("\n---\n")
    if not end:
        return None
    lines, seen = [], set()
    for line in head.split("\n"):
        name, sep, value = line.partition(": ")
        if not sep or name in seen:
            return None
        seen.add(name)
        if name == "title":
            try:
                value = json.loads(value)
            except ValueError:
                return None
            if not isinstance(value, str):
                return None
        elif name not in FIXED or not FIXED[name].fullmatch(value):
            return None
        lines.append((name, value))
    if not {"source", "id"} <= seen:
        return None
    return Header(tuple(lines), body)


def join(header: Header) -> str:
    """The copy's text again, with the title written as JSON, as the shared format writes it."""
    lines = [
        f"{name}: {json.dumps(value, ensure_ascii=False) if name == 'title' else value}"
        for name, value in header.lines
    ]
    return "---\n" + "\n".join(lines) + "\n---\n" + header.body


def names_itself(header: Header, kind: str, doc_id: str) -> bool:
    """Whether a header names this source and ID, and any link is to this doc."""
    values = dict(header.lines)
    link = values.get("link")
    return (
        values["source"] == kind
        and values["id"] == doc_id
        and (link is None or link.rsplit("/", 1)[1] == doc_id)
    )


def title(header: Header) -> str | None:
    return dict(header.lines).get("title")


def with_title(header: Header, new_title: str | None, body: str) -> Header:
    lines = tuple((n, new_title if n == "title" else v) for n, v in header.lines)
    return Header(lines, body)


def whole_names(people: People) -> dict[str, set[str]]:
    """Each name and nickname in the list, compared whole, with the placeholders that have it."""
    owners: dict[str, set[str]] = {}
    for person in people.people:
        for name in (*person.names, *person.nicknames):
            if has_letter(name):
                owners.setdefault(key(name), set()).add(person.placeholder)
    return owners


def add_authors(people: People, names: set[str]) -> int:
    """Add a person, with no Slack IDs, for each author's name nobody in the list has. In key
    order, so a run gives new people the same numbers every time. Returns how many are new."""
    owners = whole_names(people)
    next_number = max((by_number(p.placeholder) for p in people.people), default=0) + 1
    new = 0
    for name in sorted(names, key=lambda n: (key(n), n)):
        if not has_letter(name) or key(name) in owners:
            continue
        person = Person(f"[Person {next_number}]", [], [name], [])
        people.people.append(person)
        owners[key(name)] = {person.placeholder}
        next_number, new = next_number + 1, new + 1
    return new


def opted_out_names(people: People, opted_out: set[str]) -> set[str]:
    """The whole names, as compared, of everyone in the list with an opted-out Slack ID."""
    # Any whole name that isn't empty once compared, letters or not ("007").
    return {
        key(name)
        for person in people.people
        if set(person.slack_ids) & opted_out
        for name in (*person.names, *person.nicknames)
        if key(name)
    }


def by_number(placeholder: str) -> int:
    return int(PLACEHOLDER.fullmatch(placeholder)[1])


def comments_copy(
    doc_id: str,
    meta: dict,
    rows: list[dict],
    people: People,
    opted_out: set[str],
) -> tuple[str, list[Flag], int]:
    """The comments copy before the text steps run: a header, then each thread with its author
    set from the people list. Returns the text, the flags for shared names, and how many rows
    were left out because their author opted out."""
    owners = whole_names(people)
    gone = opted_out_names(people, opted_out)
    flags: list[Flag] = []
    kept, left_out = [], 0
    for row in rows:
        name = author_name(row)
        if name is not None and key(name) in gone:
            left_out += 1
            continue
        who = SOMEONE
        if name is not None and has_letter(name):
            placeholders = sorted(owners.get(key(name), ()), key=by_number)
            if len(placeholders) == 1:
                who = placeholders[0]
            elif placeholders:
                who = "[name]"
                flags.append(Flag(SHARED, name, tuple(placeholders)))
        # The ID (perm:... or gaia:...) is dropped: only the placeholder is shown.
        kept.append({**row, "actor": {"id": None, "name": who}})
    title = meta.get("title")
    title = title if isinstance(title, str) and title else doc_id
    changed = meta.get("last_changed")
    header = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"source: {COMMENTS}",
        f"id: {doc_id}",
        *([f"last_changed: {changed}"] if isinstance(changed, str) else []),
        "---",
        "",
    ]
    body = render_comments(title, kept, lambda text: text)
    note = []
    if left_out:
        note = [f"{left_out} comment(s) or reply(ies) by people who opted out were left out.", ""]
    text = "\n".join([*header, *note, body or "No comments."]).rstrip("\n") + "\n"
    return text, flags, left_out
