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

A message search (slack_search_public_and_private with content_types "messages") is JSON
whose `results` field is text, newest first, or "No results found.":

    ## Messages (2 results)
    ### Result 1 of 2
    Channel: #name (ID: C...)
    From: Name <email> (ID: U...)
    Time: ...
    Message_ts: 1790864400.000110
    Permalink: [link](https://.../archives/C.../p...?thread_ts=<parent ts>&cid=C...)
    Text:
    the message text, any number of lines

Only a thread reply's permalink has a thread_ts. `search_messages()` reads the channel, ts
and thread ts of each message, never its text, author or email.
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
# "Name <email> (U...)". The name can't hold <, > or @, so an email can never end up in it.
AUTHOR = re.compile(r"^(?P<name>[^<>@]*?)\s*(?:<[^<>]*>)?\s*\((?P<user_id>[A-Z][A-Z0-9]+)\)$")
# The detail lines at the end of a message, in the connector's exact formats. A line of text
# that only starts like one, such as "Reactions: mostly positive", stays part of the text.
META = {
    "Thread": re.compile(r"\d+ repl(?:y|ies) \(latest: .+\)"),  # "3 replies (latest: ...)"
    "Reactions": re.compile(r"[^\s,()]+ \(\d+\)(?:, [^\s,()]+ \(\d+\))*"),  # "+1 (2), eyes (1)"
    "Files": re.compile(r".+ \(ID: F[A-Z0-9]+, [^()]+\)"),  # "name (ID: F..., type, size)"
}
# Seconds, a dot, then a sequence number. Ten digits of seconds last until the year 2286;
# anything else would crash the build when it's turned into a date.
TS = re.compile(r"[0-9]{1,10}\.[0-9]+")
# A file starts at the line's start or after "), ", where the one before it ends, so its
# name can hold commas: "notes, final.txt (ID: F..., type, size), b.png (ID: F..., ...)".
FILE = re.compile(
    r"(?:^|(?<=\), ))(?P<name>.+?) \(ID: (?P<id>F[A-Z0-9]+), (?P<type>[^,()]+), [^()]+\)"
)
REACTION = re.compile(r"(?P<name>[^\s,()]+) \((?P<count>\d+)\)")
SLACK_MARKUP = re.compile(r"<([^<>\n]+)>")
CURSOR = re.compile(r"cursor:?\s*`([^`]+)`")  # "...use cursor: `bmV4dF90czox...`"
# What the connector says on the last page of a channel, a thread and a search. Anything that
# is neither this nor a cursor is page information we don't know, so it isn't a last page.
LAST_PAGE = re.compile(
    r"There are no more messages (?:available|in this thread)\.|End of results - No more pages"
)
REPLIES = re.compile(r"^(\d+) repl")  # "Thread: 3 replies (latest: ...)"
# A message's text could hold lines that look like a search's own, so a search is refused when
# the count doesn't match the results, or a result shows a line before its text twice or in
# another shape. One message's text mustn't pass as another message's channel or thread.
SEARCH_COUNT = re.compile(r"^## Messages \((\d+) results?\)[ \t]*$", re.MULTILINE)
SEARCH_EMPTY = re.compile(r"^No results found\.[ \t]*$", re.MULTILINE)
SEARCH_BLOCK = re.compile(r"^### Result \d+ of \d+[ \t]*$", re.MULTILINE)
SEARCH_LINES = {
    "Channel": re.compile(r"#[^\s()]+ \(ID: (?P<value>[A-Z0-9]+)\)"),
    "Message_ts": re.compile(r"(?P<value>[0-9]{1,10}\.[0-9]+)"),
    "Permalink": re.compile(r"\[link\]\((?P<value>[^()\s]+)\)"),
}
THREAD_TS = re.compile(r"[?&]thread_ts=([0-9]{1,10}\.[0-9]+)(?:&|$)")


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


def pages_known(tool_response) -> bool:
    """Whether a result's page information says it's the last page, or gives the next cursor."""
    pagination = (result_json(tool_response) or {}).get("pagination_info")
    if not isinstance(pagination, str):
        return False
    return LAST_PAGE.match(pagination) is not None or CURSOR.search(pagination) is not None


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


def search_messages(tool_response) -> list[tuple[str, str, str | None]] | None:
    """(channel ID, ts, thread ts or None) for each message a message search shows. None if
    the results or the page information aren't where diffdata expects them, so a last page
    can't be mistaken for one, or any message doesn't show exactly one Channel, Message_ts
    and Permalink line before its text."""
    found = result_json(tool_response)
    text = found.get("results") if found else None
    if not isinstance(text, str) or not pages_known(tool_response):
        return None
    blocks = SEARCH_BLOCK.split(text)
    count = SEARCH_COUNT.search(blocks[0])  # before the first result, out of any message's reach
    if count is None:
        return [] if len(blocks) == 1 and SEARCH_EMPTY.search(text) else None
    if int(count[1]) != len(blocks) - 1:
        return None
    rows = []
    for block in blocks[1:]:
        header, has_text, _ = block.partition("\nText:")
        if not has_text:
            return None
        values: dict[str, str] = {}
        for line in header.split("\n"):
            key, _, value = line.partition(": ")
            if key not in SEARCH_LINES:  # From and Time, which aren't kept
                continue
            match = SEARCH_LINES[key].fullmatch(value.strip())
            if match is None or key in values:
                return None
            values[key] = match["value"]
        if values.keys() != SEARCH_LINES.keys():
            return None
        thread = THREAD_TS.search(values["Permalink"])
        rows.append((values["Channel"], values["Message_ts"], thread[1] if thread else None))
    return rows


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
    if ts is None or who is None or not TS.fullmatch(ts):
        return None

    meta: dict[str, str] = {}
    while lines and (not lines[-1].strip() or detail_line(lines[-1])):
        if detail := detail_line(lines.pop()):
            meta[detail[0]] = detail[1]

    author = AUTHOR.match(who)
    if author is None:  # a shape we don't know might carry an email, so don't keep any of it
        return None
    user_id = author["user_id"]
    return Message(
        ts=ts,
        thread_ts=None,
        user_id=user_id,
        user_name=author["name"],
        bot=user_id == "USLACKBOT" or user_id.startswith("B"),
        text="\n".join(lines).strip("\n"),
        files=parse_files(meta.get("Files", "")),
        reactions=[
            {"name": r["name"], "count": int(r["count"])}
            for r in REACTION.finditer(meta.get("Reactions", ""))
        ],
        replies=int(match[1]) if (match := REPLIES.match(meta.get("Thread", ""))) else 0,
    )


def detail_line(line: str) -> tuple[str, str] | None:
    """A Thread, Reactions or Files line in the connector's format, as (key, value)."""
    key, _, value = line.rstrip().partition(": ")
    pattern = META.get(key)
    return (key, value) if pattern and pattern.fullmatch(value) else None


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
