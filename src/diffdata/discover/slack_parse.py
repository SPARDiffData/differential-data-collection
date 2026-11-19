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

# The author is everything up to the LAST "(ID) at" on the line, so a display name such as
# "Alex (UK) at home" can't end it early and pass as user "UK" (#105).
MARKER = re.compile(
    r"^(?:=== Message from (?P<who>.*\([A-Z][A-Z0-9]+\)) at .*? ==="
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


# What an opted-out person's message keeps in a saved read (#105): its place, never its words.
OPTED_OUT_NAME = "Opted out"
# The ID at the end of an author, even in a shape AUTHOR doesn't take, such as a name with @.
AUTHOR_ID = re.compile(r"\((?P<user_id>[A-Z][A-Z0-9]+)\)\s*$")
# Any user ID shown as an author: "(U...)", or "(ID: U...)" as the search tool shows it.
SHOWN_ID = re.compile(r"\((?:ID: )?(?P<user_id>[UW][A-Z0-9]{8,})\)")
# A channel stub's marker line: "=== Message from Opted out (U...) at <time> ===".
STUB_HEADER = re.compile(r"=== Message from STUB at [^=\n]* ===[ \t]*")
REPLY_MARKER = re.compile(r"--- Reply (?P<n>\d+) of (?P<of>\d+) ---")
REPLIES_TOTAL = re.compile(r"=== THREAD REPLIES \((?P<total>\d+) total\) ===")


def leave_out(
    tool_response, user_ids: set[str], tool_name: str = ""
) -> tuple[object, dict[str, set[str]], int]:
    """A channel or thread read with each message by these authors cut down to a stub: the
    author's ID, shown as "Opted out", the Time and Message TS lines, and a Thread line if
    it had one, so a thread they started is still found and read for others' replies. Their
    name, email, text, files and reactions are dropped. Returns the result, in the same
    shape, unchanged if no one was left out, the names seen for each person left out, and how
    many of their own messages the parser couldn't read before they were cut."""
    names: dict[str, set[str]] = {}
    unreadable = [0]
    if not user_ids:
        return tool_response, names, 0
    in_thread = tool_name.endswith("slack_read_thread")

    def fix_string(raw: str) -> str:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return leave_out_text(
                raw, user_ids, names, in_thread, unreadable
            )  # an error or plain text
        if isinstance(value, dict) and isinstance(value.get("messages"), str):
            messages = leave_out_text(value["messages"], user_ids, names, in_thread, unreadable)
            value = {**value, "messages": messages}
        elif isinstance(value, list):  # a result Claude Code saved to a file: the block list
            value = [{"type": "text", "text": fix_string(raw_text(value))}]
        else:
            return raw
        return json.dumps(value, ensure_ascii=False)

    # The blocks are joined first: one message, or the JSON itself, can run across two blocks.
    fixed = fix_string(raw_text(tool_response))
    if not names:
        return tool_response, names, 0
    shaped = [{"type": "text", "text": fixed}] if isinstance(tool_response, list) else fixed
    return shaped, names, unreadable[0]


def still_shows(tool_response, user_ids: set[str]) -> bool:
    """Whether any of these people still shows as an author, on a "=== Message from" or a
    "From:" line, anywhere in a result beyond their stubs: a shape leave_out didn't take,
    kept whole. The hook then saves nothing. Only author lines count, so someone else's
    message that happens to quote "(U...)" doesn't stop the export."""
    text = "\n".join(strings_in(raw_text(tool_response)))
    for line in text.split("\n"):
        line = line.strip()  # indented or with Windows line ends, it still counts
        if not line.startswith(("=== Message from ", "From: ")):
            continue
        for found in SHOWN_ID.finditer(line):
            user_id = found["user_id"]
            # Only a whole stub line, as leave_out writes it, is allowed to carry their ID:
            # "Opted out (U...)" typed into their own text doesn't count.
            stub = f"{OPTED_OUT_NAME} ({user_id})"
            if (
                user_id in user_ids
                and line != f"From: {stub}"
                and not STUB_HEADER.fullmatch(line.replace(stub, "STUB", 1))
            ):
                return True
    return False


def id_left(tool_response, user_ids: set[str]) -> bool:
    """The catch-all: whether any of these IDs is still anywhere in a result, in any form or
    block, other than a stub's "Opted out (U...)" or a mention's "<@U...". A result the filter
    couldn't cut, in whatever shape, still carries their ID somewhere, so it's refused."""
    left = json.dumps(tool_response, ensure_ascii=False)
    for user_id in user_ids:
        rest = left.replace(f"{OPTED_OUT_NAME} ({user_id})", "").replace(f"<@{user_id}", "")
        if re.search(rf"(?<![A-Z0-9]){re.escape(user_id)}(?![A-Z0-9])", rest):
            return True
    return False


def strings_in(raw: str) -> list[str]:
    """Every text in a result, JSON decoded, at any depth: what the parser would read."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    found: list[str] = []

    def walk(item) -> None:
        if isinstance(item, str):
            inner = strings_in(item) if item.lstrip()[:1] in ("{", "[", '"') else [item]
            found.extend(inner)
        elif isinstance(item, dict):
            for part in item.values():
                walk(part)
        elif isinstance(item, list):
            for part in item:
                walk(part)

    walk(value)
    return found


def opens_like_a_message(block: str, in_thread: bool) -> bool:
    """Whether the text after a marker starts the way the tool writes a message: in a thread
    read, From, Time and Message TS lines; in a channel read, a Message TS line. The TS must
    be in its own shape."""
    lines = [line.strip() for line in block.split("\n") if line.strip()]
    keys = ["From", "Time", "Message TS"] if in_thread else ["Message TS"]
    if len(lines) < len(keys):
        return False
    for line, key in zip(lines, keys, strict=False):
        if line.partition(": ")[0] != key:
            return False
    return TS.fullmatch(lines[len(keys) - 1].partition(": ")[2].strip()) is not None


def real_markers(text: str, in_thread: bool) -> list[re.Match]:
    """The marker lines the tool itself writes, in the order it writes them. A line in
    someone's text that only looks like one stays part of that text, and is cut with it: a
    channel read has only "=== Message from" lines; a thread read has one parent line, then
    one replies line, then "--- Reply n of N ---" counting up from 1. Each must be followed
    the way the tool follows it: a message marker by its header lines, the replies line by
    nothing but blank lines."""
    candidates = list(MARKER.finditer(text))
    found = []
    expected, total, parent_seen, replies_seen = 1, None, False, False
    for i, marker in enumerate(candidates):
        line = marker[0].strip()
        end = candidates[i + 1].start() if i + 1 < len(candidates) else len(text)
        block = text[marker.end() : end]
        if not in_thread:
            if marker["who"] is not None and opens_like_a_message(block, False):
                found.append(marker)
            continue
        if line == "=== THREAD PARENT MESSAGE ===":
            ok = not parent_seen and not found and opens_like_a_message(block, True)
            parent_seen = parent_seen or ok
        elif REPLIES_TOTAL.fullmatch(line):
            ok = parent_seen and not replies_seen and not block.strip()
            replies_seen = replies_seen or ok
        elif reply := REPLY_MARKER.fullmatch(line):
            ok = (
                replies_seen
                and int(reply["n"]) == expected
                and (total is None or int(reply["of"]) == total)
                and opens_like_a_message(block, True)
            )
            if ok:
                expected, total = expected + 1, int(reply["of"])
        else:
            ok = False
        if ok:
            found.append(marker)
    return found


def leave_out_text(
    text: str,
    user_ids: set[str],
    names: dict[str, set[str]],
    in_thread: bool = False,
    unreadable: list[int] | None = None,
) -> str:
    """The messages text with each block by these authors cut down to its stub."""
    markers = real_markers(text, in_thread)
    parts = [text[: markers[0].start()] if markers else text]
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        block = text[marker.end() : end]
        lines = [line for line in block.split("\n") if line.strip()]
        head = []  # the From, Time and Message TS lines before the text, as parse_block reads
        for line in lines:
            key = line.partition(": ")[0]
            if key not in ("From", "Time", "Message TS"):
                break
            head.append(line)
            if key == "Message TS":
                break
        who = marker["who"]
        if who is None:
            who = next((line[6:].strip() for line in head if line.startswith("From: ")), None)
        # Theirs if their ID is the author, or shows as an author anywhere in the marker or the
        # From line, in a shape the parser may not take.
        header = marker[0] + "\n" + "\n".join(line for line in head if line.startswith("From: "))
        found = AUTHOR_ID.search(who) if who else None
        user_id = found["user_id"] if found and found["user_id"] in user_ids else None
        if user_id is None:
            shown = [m["user_id"] for m in SHOWN_ID.finditer(header) if m["user_id"] in user_ids]
            user_id = shown[0] if shown else None
        if marker["replies"] or user_id is None:
            parts.append(text[marker.start() : end])
            continue
        if unreadable is not None and parse_block(block, marker["who"]) is None:
            unreadable[0] += 1  # theirs, and unreadable as it came: the stub can be read
        author = AUTHOR.match(who) if who else None
        if author:
            name = author["name"]
        else:  # a shape the parser doesn't take: the text before the email, or the last "("
            name = (who or "").split("<")[0].rsplit("(", 1)[0].strip()
        names.setdefault(user_id, set()).update([name] if name else [])
        stub_author = f"{OPTED_OUT_NAME} ({user_id})"
        if marker["who"] is None:
            stub = [marker[0], f"From: {stub_author}"]
        else:
            start = marker.start("who") - marker.start()
            stub = [
                marker[0][:start] + stub_author + marker[0][marker.end("who") - marker.start() :]
            ]
        stub += [line for line in head if line.startswith(("Time: ", "Message TS: "))]
        details = lines[len(head) :]
        while details and detail_line(details[-1]):
            if detail_line(details[-1])[0] == "Thread" and (
                count := REPLIES.match(detail_line(details[-1])[1])
            ):
                # Rebuilt from the count alone: the "latest" part could be their own words.
                n = int(count[1])
                stub.append(f"Thread: {n} {'reply' if n == 1 else 'replies'} (latest: -)")
            details.pop()
        trailing = len(block) - len(block.rstrip("\n"))
        parts.append("\n".join(stub) + "\n" * max(trailing, 1))
    return "".join(parts)
