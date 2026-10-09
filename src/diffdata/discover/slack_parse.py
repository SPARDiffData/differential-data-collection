"""Turn the Slack connector's results into message records (DIS-1).

The connector returns formatted text, not structured data. A saved tool result is a list
of text blocks holding JSON, whose `messages` field is that text. In detailed mode:

  slack_read_channel, newest first:
    Channel: #name (C...)
    === Message from Name <email> (U...) at 2026-10-01 09:14:00 CDT ===
    Message TS: 1790864040.000100
    the message text, any number of lines
    Thread: 3 replies (latest: ...)
    Reactions: +1 (2)

  slack_read_thread:
    === THREAD PARENT MESSAGE ===
    From: Name <email> (U...)
    Time: ...
    Message TS: ...
    the message text
    === THREAD REPLIES (3 total) ===
    --- Reply 1 of 3 ---
    From: ...   (then Time, Message TS and the text, as for the parent)

Bots have no email, as in "Slackbot (USLACKBOT)". Attached files come as
"Files: name (ID: F..., type, size)". Author emails are read past and never kept: the raw
inbox still has them until scrubbing (SCR-2).
"""

import hashlib
import json
import re
from dataclasses import dataclass

MARKER = re.compile(
    r"^(?:=== Message from (?P<who>.*?\([A-Z][A-Z0-9]+\)) at .*? ==="
    r"|=== THREAD PARENT MESSAGE ==="
    r"|--- Reply \d+ of \d+ ---"
    r"|(?P<replies>=== THREAD REPLIES \(\d+ total\) ===))[ \t]*$",
    re.MULTILINE,
)
AUTHOR = re.compile(r"^(?P<name>.*?)(?: <[^<>]*>)? \((?P<user_id>[A-Z][A-Z0-9]+)\)$")
META = re.compile(r"^(?P<key>Thread|Reactions|Files): (?P<value>.*)$")
FILE = re.compile(r"(?P<name>[^,]+?) \(ID: (?P<id>F[A-Z0-9]+), (?P<type>[^,()]+), [^()]+\)")
REACTION = re.compile(r"(?P<name>[^\s,()]+) \((?P<count>\d+)\)")
SLACK_MARKUP = re.compile(r"<([^<>\n]+)>")
CURSOR = re.compile(r"cursor:?\s*`([^`]+)`")  # "...use cursor: `bmV4dF90czox...`"
REPLIES = re.compile(r"^(\d+) repl")  # "Thread: 3 replies (latest: ...)"


@dataclass
class Message:
    ts: str
    thread_ts: str | None  # the parent's ts, for a thread reply only
    user_id: str
    user_name: str
    bot: bool
    text: str  # as Slack sent it, markup and all
    files: list[dict]
    reactions: list[dict]
    replies: int = 0  # from a channel read's Thread line; not part of the content

    def content_hash(self) -> str:
        """A new hash means a new version of the message. Reactions don't count."""
        content = json.dumps({"text": self.text, "files": self.files}, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def raw_text(tool_response) -> str:
    """The text of a tool result: a list of text blocks, or a plain string."""
    blocks = tool_response if isinstance(tool_response, list) else [tool_response]
    return "".join(
        block if isinstance(block, str) else block.get("text") or ""
        for block in blocks
        if isinstance(block, str | dict)
    )


def result_json(tool_response) -> dict | None:
    """The JSON inside a tool result, or None if it isn't JSON."""
    try:
        inner = json.loads(raw_text(tool_response))
    except json.JSONDecodeError:
        return None
    if isinstance(inner, list):  # a result Claude Code saved to a file can hold the block list
        return result_json(inner)
    return inner if isinstance(inner, dict) else None


def result_text(tool_response) -> str:
    """The `messages` text inside a saved tool result."""
    inner = result_json(tool_response)
    if inner is None:
        return raw_text(tool_response)
    messages = inner.get("messages", "")
    return messages if isinstance(messages, str) else ""


def next_cursor(tool_response) -> str | None:
    """The cursor for the next page, if the result says there is one."""
    pagination = (result_json(tool_response) or {}).get("pagination_info", "")
    match = CURSOR.search(pagination if isinstance(pagination, str) else "")
    return match[1] if match else None


def parse_result(tool_name: str, tool_input: dict, tool_response) -> tuple[list[Message], int]:
    """The messages in one saved tool result, and how many blocks couldn't be read."""
    in_thread = tool_name.endswith("slack_read_thread")
    parent_ts = tool_input.get("message_ts") if in_thread else None
    text = result_text(tool_response)
    markers = list(MARKER.finditer(text))
    messages, skipped = [], 0
    for i, marker in enumerate(markers):
        if marker["replies"]:
            continue
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        message = parse_block(text[marker.end() : end], marker["who"])
        if message is None:
            skipped += 1
            continue
        if marker[0].startswith("---"):
            message.thread_ts = parent_ts
        messages.append(message)
    return messages, skipped


def parse_block(block: str, who: str | None) -> Message | None:
    """One message: the From, Time and Message TS lines, then the text, then Thread,
    Reactions and Files lines at the end."""
    lines = block.strip("\n").split("\n")
    ts = None
    while lines and ts is None:
        key, _, value = lines.pop(0).partition(": ")
        if key == "From":
            who = value.strip()
        elif key == "Message TS":
            ts = value.strip()
        elif key != "Time":
            return None
    if ts is None or who is None:
        return None

    meta: dict[str, str] = {}
    while lines and (not lines[-1].strip() or META.match(lines[-1])):
        if match := META.match(lines.pop()):
            meta[match["key"]] = match["value"]

    author = AUTHOR.match(who)
    user_id = author["user_id"] if author else ""
    return Message(
        ts=ts,
        thread_ts=None,
        user_id=user_id,
        user_name=author["name"] if author else who,
        bot=user_id == "USLACKBOT" or user_id.startswith("B"),
        text="\n".join(lines).strip("\n"),
        files=parse_files(meta.get("Files", "")),
        reactions=[
            {"name": r["name"], "count": int(r["count"])}
            for r in REACTION.finditer(meta.get("Reactions", ""))
        ],
        replies=int(match[1]) if (match := REPLIES.match(meta.get("Thread", ""))) else 0,
    )


def parse_files(value: str) -> list[dict]:
    if not value:
        return []
    files = [{"id": f["id"], "name": f["name"], "type": f["type"]} for f in FILE.finditer(value)]
    # Only single files have been seen so far. Keep a line we can't split rather than lose it.
    return files or [{"id": "", "name": value, "type": ""}]


def permalink(workspace_url: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    """A message's Slack link: the ts with its dot removed, prefixed with 'p'."""
    link = f"{workspace_url}/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts:
        link += f"?thread_ts={thread_ts}&cid={channel_id}"
    return link


def to_markdown(text: str, people: dict[str, str], channels: dict[str, str]) -> str:
    """Slack markup to readable markdown: @Name, #channel and [label](url).

    people and channels map IDs to names, for mentions that arrive without a name.
    """

    def replace(match: re.Match) -> str:
        target, _, label = match[1].partition("|")
        if target.startswith("@"):
            return "@" + (label or people.get(target[1:]) or target[1:])
        if target.startswith("#"):
            return "#" + (label or channels.get(target[1:]) or target[1:])
        if target.startswith("!"):  # <!here>, <!channel>, <!subteam^S...|@team>
            return label or "@" + target[1:].split("^")[0]
        if target.startswith("mailto:"):
            return label or target.removeprefix("mailto:")
        return f"[{label}]({target})" if label and label != target else target

    text = SLACK_MARKUP.sub(replace, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
