"""The reference log (DIS-2, DIS-3, COL-7): every source the saved Slack data points to, once
each, with every place it was referenced.

The Slack export's build calls `build()` last. It reads only what's already saved, and
fetches nothing:
  - links and attachments in every saved message version (the Slack store)
  - each message's last saved read, from the inbox, to mark whether that read still has the
    link. Not the store's newest line: the store drops a version that matches an older one,
    so after "no link, link, no link" its newest line still has the link.
  - Slackbot's canvas-tab notices
  - links in every saved canvas version

Each source has one key, which never changes:
  slack-file:F...   a Slack canvas or attachment, by its file ID
  google:<id>       a Google Doc, Sheet, Slides or Drive file, by its file ID
  url:<address>     any other web link, as written but without its # part
Only web links are logged: mailto: and other kinds are skipped, so no email address becomes
an entry. A Google Drive folder and a link to a Slack message are ordinary links, because
approving a folder isn't approving its files.

A Slack file is a canvas when a canvas link (/docs/) or a tab notice names it, config.toml
lists it, it has saved canvas reads, or its attachment type says canvas. Otherwise it's an
attachment. A source's type only ever becomes more exact, such as attachment to canvas.

The log only grows. A build adds sources and places, and updates states, titles and the
latest-read marks, but never removes anything, so a reference stays after the data behind it
is deleted. A log diffdata can't read is left as it was. The log is private, like the store
it comes from, and is never released or pushed.

A source's state:
  collected      listed in config.toml, and read
  approved       listed, not read yet
  unreachable    listed, but a fetcher couldn't reach it, for the reason in `unreachable`
                 (COL-7). Nothing sets that yet: it's kept for the fetchers.
  withdrawn      collected or listed before, but not listed now
  history-only   referenced only from channels or canvases no longer opted in
  taken-back     every link to it from an opted-in place was edited out
  new            any other canvas or Google file
  not-collected  attachments (never downloaded) and ordinary links (logged only)

Under the data folder:
  references/references.jsonl   one source per line, by key, with its places
  references/references.md      the same, to read, grouped by type
"""

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from diffdata import __version__
from diffdata.collect import slack_canvas
from diffdata.common.config import Config
from diffdata.common.documents import documents_dir, keep_changes, stamp, write_if_changed

CANVAS = "slack-canvas"
ATTACHMENT = "slack-attachment"
GOOGLE_FILE = "google-file"
LINK = "link"
# The Google type a docs.google.com link names, by the word in its path.
GOOGLE_TYPES = {
    "document": "google-doc",
    "spreadsheets": "google-sheet",
    "presentation": "google-slides",
}
# Each type's heading in references.md, in the order it lists them.
GROUPS = {
    CANVAS: "Slack canvases",
    "google-doc": "Google Docs",
    "google-sheet": "Google Sheets",
    "google-slides": "Google Slides",
    GOOGLE_FILE: "Google Drive files",
    ATTACHMENT: "Slack attachments",
    LINK: "Other links",
}
# Types more exact than another a source can show: a Slack file linked as a canvas, or a
# Google file linked as a Doc.
EXACT = {CANVAS, *GOOGLE_TYPES.values()}
# An attachment whose type says it's a canvas. A guess: no live read has shown one yet.
CANVAS_FILE_TYPES = {"canvas", "quip", "application/vnd.slack-docs"}

COLLECTED = "collected"
APPROVED = "approved"
UNREACHABLE = "unreachable"
WITHDRAWN = "withdrawn"
HISTORY_ONLY = "history-only"
TAKEN_BACK = "taken-back"
NEW = "new"
NOT_COLLECTED = "not-collected"
# A source in one of these states was opted in once, so leaving config.toml withdraws it.
OPTED_IN = {COLLECTED, APPROVED, UNREACHABLE, WITHDRAWN}
STATE_TEXT = {
    COLLECTED: "Collected.",
    APPROVED: "Listed in config.toml, not collected yet.",
    UNREACHABLE: "Listed in config.toml, but diffdata can't reach it: {reason}.",
    WITHDRAWN: "Withdrawn: collected or listed before, but not listed now.",
    HISTORY_ONLY: "History only: referenced only from channels or canvases no longer opted in.",
    TAKEN_BACK: "Taken back: every link to it was edited out.",
    NEW: "New: not listed in config.toml.",
    NOT_COLLECTED: "Not collected: {reason}.",
}

# https://<workspace>.slack.com/docs/T.../F...
SLACK_CANVAS = re.compile(r"https://[a-z0-9-]+\.slack\.com/docs/T[A-Z0-9]+/(F[A-Z0-9]+)(?=[/?#]|$)")
# https://<workspace>.slack.com/files/U.../F.../<name>, or
# https://files.slack.com/files-pri/T...-F.../<name>
SLACK_FILE = re.compile(
    r"https://(?:[a-z0-9-]+\.slack\.com/files/[UW][A-Z0-9]+/"
    r"|files\.slack\.com/files-(?:pri|tmb)/T[A-Z0-9]+-)(F[A-Z0-9]+)(?=[/?#]|$)"
)
# https://docs.google.com/document/d/<id>/edit, and the same for spreadsheets and presentation.
# Real IDs are far longer than 10, which keeps out a published doc's /d/e/... link.
GOOGLE_DOC = re.compile(
    r"https://docs\.google\.com/(document|spreadsheets|presentation)/(?:u/\d+/)?d/"
    r"([A-Za-z0-9_-]{10,})"
)
# https://drive.google.com/file/d/<id>/view. A folder (/drive/folders/) is an ordinary link.
DRIVE_FILE = re.compile(r"https://drive\.google\.com/(?:u/\d+/)?file/d/([A-Za-z0-9_-]{10,})")
# Slack's markup, <address> or <address|text>, which also wraps mentions (<@U...>), and in
# canvases, some HTML (<br>). Canvas text is markdown: [text](address), or ![text](address)
# for an image. An address can also be written out, and punctuation at its end is the
# sentence's.
SLACK_LINK = re.compile(r"<([^<>\n]+)>")
MARKDOWN_LINK = re.compile(r"!?\[([^\]\n]*)\]\(([^()\s]+)\)")
BARE_LINK = re.compile(r"https?://[^\s<>()\[\]|\"'`]+")
WEB = ("http://", "https://")

# What every source and every place in the log has, in the order written.
SOURCE_FIELDS = (
    "key",
    "type",
    "state",
    "reason",
    "title",
    "link",
    "file_type",
    "first_seen",
    "unreachable",
    "places",
)
PLACE_FIELDS = (
    "place",
    "channel_id",
    "ts",
    "thread_ts",
    "canvas_id",
    "link",
    "who",
    "at",
    "via",
    "latest",
)


@dataclass(frozen=True)
class Found:
    """One reference, before it's merged into the log."""

    key: str
    type: str
    via: str  # link, attachment or tab notice
    link: str | None = None
    title: str | None = None  # the link's text, or the attachment's name
    file_type: str | None = None  # an attachment's type, as Slack gives it


def log_dir(data: Path) -> Path:
    return data / "references"


def unescape(text: str) -> str:
    """Slack writes &, < and > in text as &amp;, &lt; and &gt;."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def links(text: str) -> list[tuple[str, str]]:
    """Each web link in a message's or a canvas's text, as (address, link text). The text
    is "" when there's none, or when it only repeats the address."""
    found: list[tuple[str, str]] = []

    def keep(address: str, label: str = "") -> str:
        address, label = unescape(address.strip()), unescape(label.strip())
        if address.lower().startswith(WEB):
            found.append((address, "" if label == address else label))
        return " "  # so a link isn't found again as a written-out address

    def slack(match: re.Match) -> str:
        address, _, label = match[1].partition("|")
        return keep(address, label)

    text = SLACK_LINK.sub(slack, text)
    text = MARKDOWN_LINK.sub(lambda match: keep(match[2], match[1]), text)
    for match in BARE_LINK.finditer(text):
        keep(match[0].rstrip(".,;:!?"))
    return found


def source_of(address: str) -> tuple[str, str]:
    """A web link's source: its key and type."""
    if match := SLACK_CANVAS.match(address):
        return f"slack-file:{match[1]}", CANVAS
    if match := SLACK_FILE.match(address):
        return f"slack-file:{match[1]}", ATTACHMENT
    if match := GOOGLE_DOC.match(address):
        return f"google:{match[2]}", GOOGLE_TYPES[match[1]]
    if match := DRIVE_FILE.match(address):
        return f"google:{match[1]}", GOOGLE_FILE
    return "url:" + address.partition("#")[0], LINK


def in_text(text: str) -> list[Found]:
    """The sources a message's or a canvas's text links to."""
    return [
        Found(*source_of(address), "link", address, label or None) for address, label in links(text)
    ]


def in_message(record: dict) -> list[Found]:
    """The sources one saved message version references: its links, its attachments, and the
    canvas a Slackbot tab notice names."""
    found = in_text(record["text"] or "")
    for file in record["files"]:
        if not file["id"]:
            continue  # a Files line diffdata couldn't split, so there's no ID to key it by
        kind = CANVAS if file["type"].lower() in CANVAS_FILE_TYPES else ATTACHMENT
        key, name, file_type = f"slack-file:{file['id']}", file["name"], file["type"]
        found.append(
            Found(key, kind, "attachment", title=name or None, file_type=file_type or None)
        )
    for canvas in slack_canvas.tab_notices([record]):
        found.append(Found(f"slack-file:{canvas}", CANVAS, "tab notice"))
    return found


def slack_time(ts: str) -> str:
    return datetime.fromtimestamp(int(ts.split(".")[0]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_messages(
    stores: dict[str, list[dict]], latest: dict[tuple[str, str], dict]
) -> Iterator[tuple[Found, dict]]:
    """Each reference in every saved message version, with its place."""
    for channel_id, store in sorted(stores.items()):
        newest = {record["ts"]: record for record in store}  # if the inbox no longer has it
        still: dict[str, set[str]] = {}
        for record in store:
            ts = record["ts"]
            if ts not in still:
                read = latest.get((channel_id, ts), newest[ts])
                still[ts] = {found.key for found in in_message(read)}
            place = {
                "place": f"message:{channel_id}:{ts}",
                "channel_id": channel_id,
                "ts": ts,
                "thread_ts": record["thread_ts"],
                "canvas_id": None,
                "link": record["permalink"],
                "who": {"id": record["user_id"], "name": record["user_name"]},
                "at": slack_time(ts),
            }
            for found in in_message(record):
                yield found, place | {"via": [found.via], "latest": found.key in still[ts]}


def in_canvases(data: Path) -> tuple[list[tuple[Found, dict]], dict[str, str], set[str]]:
    """Each reference in every saved canvas version, with its place; each canvas's title
    from its latest text; and the canvases with saved reads."""
    found_here: list[tuple[Found, dict]] = []
    titles: dict[str, str] = {}
    read: set[str] = set()
    for folder in sorted(
        p for p in (documents_dir(data) / slack_canvas.SOURCE).glob("*") if p.is_dir()
    ):
        canvas_id = folder.name
        if any(folder.glob("raw/*/*.json")):
            read.add(canvas_id)
        versions, unreadable = slack_canvas.read_versions(folder)
        if unreadable or not versions:
            continue  # the canvas build leaves such a canvas as it was, and so does this
        kept = keep_changes(versions)
        newest = kept[-1][1]
        if (title := slack_canvas.title(newest, canvas_id)) != canvas_id:
            titles[canvas_id] = title
        still = {found.key for found in in_text(newest)}
        for event, text in kept:  # oldest first, so a place's time is its first version's
            place = dict.fromkeys(PLACE_FIELDS) | {
                "place": f"canvas:{canvas_id}",
                "canvas_id": canvas_id,
                "at": event["at"] or event["seen_at"],
            }
            for found in in_text(text):
                found_here.append(
                    (found, place | {"via": [found.via], "latest": found.key in still})
                )
    return found_here, titles, read


def add(log: dict[str, dict], found: Found, place: dict) -> bool:
    """Merge one reference into the log: its source, then its place. True if the source is
    new to the log."""
    entry = log.get(found.key)
    added = entry is None
    if added:
        entry = log[found.key] = dict.fromkeys(SOURCE_FIELDS) | {
            "key": found.key,
            "type": found.type,
            "places": {},
        }
    if found.type in EXACT:
        entry["type"] = found.type
    entry["link"] = entry["link"] or found.link
    entry["title"] = entry["title"] or found.title
    entry["file_type"] = entry["file_type"] or found.file_type
    old = entry["places"].get(place["place"])
    if old is None:
        entry["places"][place["place"]] = place
        return added
    old["via"] = sorted(set(old["via"]) | set(place["via"]))
    old["latest"] = place["latest"]
    if place["thread_ts"] and not old["thread_ts"]:  # a thread read's copy says which thread
        old["thread_ts"], old["link"] = place["thread_ts"], place["link"]
    return added


def state(entry: dict, listed: set[str], read: set[str], opted_in) -> tuple[str, str | None]:
    """A source's state, and the reason for one that isn't collected. `opted_in` says
    whether a place is still opted in."""
    if entry["type"] == ATTACHMENT:
        return NOT_COLLECTED, "attachments are never downloaded"
    if entry["type"] == LINK:
        return NOT_COLLECTED, "ordinary links are logged only"
    if entry["key"] in listed:
        if entry["key"] in read:
            return COLLECTED, None
        return (UNREACHABLE, entry["unreachable"]) if entry["unreachable"] else (APPROVED, None)
    if entry["key"] in read or entry["state"] in OPTED_IN:
        return WITHDRAWN, None
    places = [place for place in entry["places"].values() if opted_in(place)]
    if not places:
        return HISTORY_ONLY, None
    if not any(place["latest"] for place in places):
        return TAKEN_BACK, None
    return NEW, None


def build(
    config: Config,
    stores: dict[str, list[dict]],
    latest: dict[tuple[str, str], dict],
    channels: dict[str, str],
) -> Counter:
    """Merge every reference in the saved data into the log, then write the log and its view.
    stores: each channel's Slack store, by channel ID. latest: each message's last saved
    read, by (channel ID, ts). channels: channel ID to name. Returns counts only."""
    data = config.data_dir
    path = log_dir(data) / "references.jsonl"
    log = load(path)
    if log is None:
        return Counter(unreadable=1)
    counts: Counter = Counter()
    from_canvases, titles, read = in_canvases(data)
    for found, place in [*in_messages(stores, latest), *from_canvases]:
        counts["added"] += add(log, found, place)

    listed = {f"slack-file:{canvas}" for canvas in config.slack_canvases}
    read_keys = {f"slack-file:{canvas}" for canvas in read}

    def opted_in(place: dict) -> bool:
        if place["canvas_id"]:
            return place["canvas_id"] in config.slack_canvases
        return channels.get(place["channel_id"]) in config.slack_channels

    for key, entry in log.items():
        if key in listed or key in read_keys:
            entry["type"] = CANVAS
        if (canvas := key.removeprefix("slack-file:")) in titles:
            entry["title"] = titles[canvas]  # a read's title is newer than any link's text
        entry["state"], entry["reason"] = state(entry, listed, read_keys, opted_in)
        entry["first_seen"] = min(place["at"] for place in entry["places"].values())
        counts["google" if entry["type"].startswith("google") else entry["type"]] += 1
        counts.update(sources=1, places=len(entry["places"]), new=entry["state"] == NEW)

    entries = [
        {field: entry[field] for field in SOURCE_FIELDS}
        | entry
        | {"places": sorted(entry["places"].values(), key=lambda p: (p["at"], p["place"]))}
        for _, entry in sorted(log.items())
    ]
    canvas_titles = {
        entry["key"].removeprefix("slack-file:"): entry["title"]
        for entry in entries
        if entry["type"] == CANVAS and entry["title"]
    } | titles
    try:
        write_log(path, "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries))
        write_if_changed(log_dir(data) / "references.md", render(entries, channels, canvas_titles))
    except OSError:
        return Counter(failed=1)
    return counts


def readable(entry) -> bool:
    """Whether a log line has what the build relies on."""
    return (
        isinstance(entry, dict)
        and set(SOURCE_FIELDS) <= entry.keys()
        and isinstance(entry["key"], str)
        and entry["type"] in GROUPS
        and isinstance(entry["places"], list)
        and entry["places"] != []  # every source was found somewhere
        and all(
            isinstance(place, dict)
            and set(PLACE_FIELDS) <= place.keys()
            and isinstance(place["place"], str)
            and isinstance(place["at"], str)
            and isinstance(place["via"], list)
            and all(
                isinstance(place[name], str | None)
                for name in ("channel_id", "canvas_id", "thread_ts")
            )
            for place in entry["places"]
        )
    )


def load(path: Path) -> dict[str, dict] | None:
    """The log's sources by key, each with its places by place. Empty if there's no log yet,
    and None if diffdata can't read it, so it's never overwritten."""
    if not path.exists():
        return {}
    log: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not readable(entry) or entry["key"] in log:
                return None
            log[entry["key"]] = entry | {
                "places": {place["place"]: place for place in entry["places"]}
            }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return log


def write_log(path: Path, text: str) -> None:
    """Write the log through a temporary file, so a write cut short leaves the old log whole."""
    body = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == body:
        return
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


def one_line(text: str) -> str:
    """Line breaks and runs of spaces become one space, so a title can't start a new line."""
    return " ".join(str(text).split())


def render(entries: list[dict], channels: dict[str, str], canvas_titles: dict[str, str]) -> str:
    """references.md: a summary, then each type's sources, oldest first."""
    groups = {kind: [entry for entry in entries if entry["type"] == kind] for kind in GROUPS}
    lines = [
        "---",
        "source: slack",
        f"sources: {len(entries)}",
        f"places: {sum(len(entry['places']) for entry in entries)}",
        f"tool: diffdata {__version__}",
        "---",
        "",
        "# References",
        "",
        "Every source the saved Slack data points to, once each, with every place it was "
        "referenced. diffdata builds this from data already saved, and fetches nothing here. "
        '"Edited out" means the latest saved read of that message or canvas no longer has '
        "the link.",
        "",
    ]
    for kind, group in groups.items():
        if group:
            states = Counter(entry["state"] for entry in group)
            listed = ", ".join(
                f"{states[name]} {name.replace('-', ' ')}" for name in STATE_TEXT if states[name]
            )
            lines.append(f"- {GROUPS[kind]}: {len(group)} ({listed})")
    for kind, group in groups.items():
        if not group:
            continue
        lines += ["", f"## {GROUPS[kind]}"]
        for entry in sorted(group, key=lambda e: (e["first_seen"], e["key"])):
            lines += [
                "",
                f"### {one_line(entry['title'] or entry['link'] or entry['key'])}",
                "",
                STATE_TEXT[entry["state"]].format(reason=entry["reason"]),
                "",
                f"- Key: `{entry['key']}`",
            ]
            if entry["link"]:
                lines.append(f"- Link: {entry['link']}")
            if entry["file_type"]:
                lines.append(f"- File type: {one_line(entry['file_type'])}")
            lines.append("- Referenced from:")
            lines += [f"  - {where(place, channels, canvas_titles)}" for place in entry["places"]]
    return "\n".join(lines) + "\n"


def where(place: dict, channels: dict[str, str], canvas_titles: dict[str, str]) -> str:
    """One place, on one line: when, where, who, how, and a link to the message."""
    if place["canvas_id"]:
        canvas = place["canvas_id"]
        parts = [stamp(place["at"]), f'canvas "{one_line(canvas_titles.get(canvas, canvas))}"']
    else:
        who = place["who"] or {}
        parts = [
            stamp(place["at"]),
            f"#{channels.get(place['channel_id'], place['channel_id'])}",
            one_line(who.get("name") or who.get("id") or "someone"),
        ]
    parts.append(", ".join(place["via"]))
    if place["link"]:
        parts.append(f"[message]({place['link']})")
    if not place["latest"]:
        parts.append("edited out")
    return " · ".join(parts)
