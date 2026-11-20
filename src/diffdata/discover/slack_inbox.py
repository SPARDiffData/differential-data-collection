"""The Slack inbox: raw connector results, saved unchanged, one file per tool call (DIS-1).

For the pilot there's no Slack app (DIS-6). The /slack-export command reads Slack through
Claude's Slack connector, and a hook passes each result to `save_from_hook()`, so Claude
never retypes it. A result is saved only while an export run is open, and only for the
channels that run named, all of which must be opted in through config.toml (CON-1).
A channel read must show that channel's opted-in name and ID in its header, and is saved
only then. A channel counts as checked in a run once its read is saved in that run's inbox
folder, and a thread is saved only after that. Each save writes its own file, so two reads
finishing together can't undo each other's check (#21).
A result too big for Claude's context reaches the hook only as a notice naming the file
Claude Code saved it in, so `save` reads that file, and only from the session's own
tool-results folder. After each save, the hook tells Claude what it needs to carry on.
Canvas reads go to collect/slack_canvas.py (COL-2), and only for the canvases every run
names: those listed in config.toml, and those approved at review (consent/approvals.py),
as they stood when the run opened.
A search must ask for files only or messages only, leave out DMs, and name one channel the
run names, after that channel's read has passed its check. A files search goes to the
canvas code, which keeps only the listed canvases' sizes. A message search, which finds
new replies in older threads, is saved as its channel, start, cursors and the threads it
found replies in, never its text, and is refused if any result is from another channel.
A second hook runs `check()` before each read or search, so one the run doesn't name is
stopped before anything is fetched, as is any Slack read the export never makes. Both hooks
set onFailure: block, so a hook that can't start or times out stops the read too.
`begin` says where each channel's read and search start, from slack_state.py.

Messages by people listed in `slack_opted_out` in config.toml are cut down before a channel or
thread read is saved: each keeps its author's ID, shown as "Opted out", its time and TS, and a
Thread line if it had one, so their threads are still found and read for others' replies and
the page checks still hold. Their name, email, text, files and reactions are never saved
(#105). Their IDs and names go in slack/left_out.json, for the scrub only.

Layout under the data folder:
  slack/run.json                                   the open run, if any
  slack/runs/<run-id>.json                         every closed run, and why it closed
  slack/state.json                                 each channel's start point (slack_state.py)
  slack/inbox/<channel-id>/<run-id>/<tool>-<call>.json   one raw channel or thread read
  slack/inbox/<channel-id>/<run-id>/search_messages-<call>.json   one message search's threads
  slack/left_out.json                              who opted out, by ID and name, for the scrub
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.collect import slack_canvas
from diffdata.common.config import Config, ConfigError, channel_name, load_config
from diffdata.common.paths import UnsafeDataDirError
from diffdata.consent import approvals
from diffdata.discover import slack_state
from diffdata.discover.slack_parse import (
    OPTED_OUT_NAME,
    id_left,
    leave_out,
    next_cursor,
    pages_known,
    parse_result,
    raw_text,
    real_markers,
    result_json,
    result_text,
    search_messages,
    still_shows,
)

# A run that crashed or was forgotten must not leave the hook saving, so runs expire.
RUN_MINUTES = 60
# The connector tools whose results go to the inbox unchanged. The command's hook matches
# these two, canvas reads, which go to the canvas code, and the search: a files search goes
# to the canvas code, and a message search is saved as its thread list.
SAVED_TOOLS = ("slack_read_channel", "slack_read_thread")
# When a result is too big for Claude's context, Claude Code saves it to a file in the
# session's tool-results folder, and the hook gets only a notice naming that file.
OVERFLOW = re.compile(r"saved to:?\s+(?P<path>.+?\.(?:txt|json))\b", re.IGNORECASE)
# Public channels start with C, older private ones with G. DMs (D) need both people's
# consent first (CON-5), so they're refused.
CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{6,}")
# The reads `check()` lets through only when the open run names what they read, and the
# input field that names it. The command's PreToolUse hook matches these and the search.
CHECKED_READS = {
    "slack_read_channel": "channel_id",
    "slack_read_thread": "channel_id",
    "slack_read_canvas": "canvas_id",
}
# The command's PreToolUse hook matches every Slack tool, so `check()` stops any the export
# never uses before it runs, such as slack_read_file, which would download an attachment.
# Finding a channel's ID comes before the run opens, so that one is always let through.
FIND_CHANNEL = "slack_search_channels"
# The search tool. It searches DMs too unless told not to, and every file or message the
# person can see unless its filter names a channel.
SEARCH_TOOL = "slack_search_public_and_private"
SEARCH_CHANNEL_TYPES = {"public_channel", "private_channel"}
SEARCH_FILTER = re.compile(r"in:<#(?P<id>[CG][A-Z0-9]{6,})>")
# What a search may look for, one kind at a time. The tool's default isn't documented, so a
# search that doesn't say is refused rather than guessed at.
SEARCH_KINDS = ("files", "messages")
# The only fields a search may set. Keywords or a query could carry a filter of their own,
# such as a second channel, so they're left out (natural_language_query only as "").
SEARCH_FIELDS = {
    "content_types",
    "channel_types",
    "filters",
    "natural_language_query",
    "response_format",
    "include_context",
    "limit",
    "cursor",
}
# A message search also says where to start, keeps bots' replies, and sorts by time.
MESSAGE_SEARCH_FIELDS = SEARCH_FIELDS | {"after", "include_bots", "sort"}
UNIX_TIME = re.compile(r"[0-9]{1,10}")
# A channel read starts with "Channel: #name (ID)". The hook checks it against the run, so a
# wrong ID chosen by Claude can't bring in a channel that wasn't opted in (CON-1).
CHANNEL_HEADER = re.compile(r"Channel: #(?P<name>[^\s()]+) \((?P<id>[A-Z0-9]+)\)")


class SlackExportError(Exception):
    """An export step can't go ahead. The message says why, in plain words."""


@dataclass
class Run:
    run_id: str
    started_at: str
    expires_at: str
    channels: dict[str, str]  # channel ID -> channel name
    closed_at: str | None = None
    closed_why: str | None = None
    # A full run reads every channel from the beginning and every listed canvas.
    full: bool = False
    # The canvases listed in config.toml or approved at review when the run opened. Only
    # these reads are saved.
    canvases: list[str] = field(default_factory=list)
    # The canvases `canvases-due` said to read, or None if it hasn't run.
    due: list[str] | None = None


def utc_iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slack_dir(data: Path) -> Path:
    return data / "slack"


def run_path(data: Path) -> Path:
    return slack_dir(data) / "run.json"


def inbox_dir(data: Path) -> Path:
    return slack_dir(data) / "inbox"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file, so a write cut short leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".saving-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def left_out_path(data: Path) -> Path:
    return slack_dir(data) / "left_out.json"


def read_left_out(data: Path) -> dict[str, list[str]]:
    """Each opted-out person's Slack ID and the display names seen for them (#105). Empty if
    there's no file yet. A file that's there but can't be read stops the run: the names in it
    may exist nowhere else, and without them the scrub couldn't replace mentions of them."""
    path = left_out_path(data)
    if not path.exists():
        return {}
    damaged = SlackExportError(
        "slack/left_out.json in the data folder can't be read, so nothing was built or "
        "scrubbed. It lists the people who opted out, for the scrub. Put back a copy, or ask "
        "the project team."
    )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise damaged from error
    if not isinstance(found, dict) or not all(
        isinstance(user_id, str)
        and isinstance(names, list)
        and all(isinstance(n, str) for n in names)
        for user_id, names in found.items()
    ):
        raise damaged
    return found


def applied_path(data: Path) -> Path:
    return slack_dir(data) / "opt_out_applied.json"


def opt_out_stamp(user_ids) -> str:
    """A fingerprint of an opt-out list, so the scrub can tell which list the last finished
    build applied without the file naming anyone."""
    joined = "\n".join(sorted(set(user_ids)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def applied_stamp(data: Path) -> str:
    """The stamp of the opt-out list the last finished build applied. No file means no list
    was ever applied, the same as an empty one."""
    try:
        found = json.loads(applied_path(data).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return opt_out_stamp([])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""  # matches no list, so the scrub waits for a build
    stamp = found.get("applied") if isinstance(found, dict) else None
    return stamp if isinstance(stamp, str) else ""


# How long a save waits for another to finish adding to left_out.json. Each holds it for
# one read and one small write, so a wait this long means one was cut off mid-way.
LEFT_OUT_WAIT_SECONDS = 10.0


@contextmanager
def left_out_locked(data: Path) -> Iterator[None]:
    """Hold slack/left_out.json for one read, add and write. When Claude reads several things
    at once, two hooks can finish together: without this, each would write back only its own
    names over the other's, and a name lost there would let plain-text mentions of that
    person through the scrub. If another holds it, wait for it, briefly."""
    lock = left_out_path(data).with_name("left_out.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LEFT_OUT_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError as e:
            if time.monotonic() > deadline:
                raise SlackExportError(
                    "another save held slack/left_out.json for too long. If no Slack export "
                    "is running, delete slack/left_out.json.lock in the data folder"
                ) from e
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def record_left_out(data: Path, seen: dict[str, set[str]]) -> None:
    """Add these people and names to slack/left_out.json. It only grows: someone taken off
    the list later is still pseudonymised, never shown by name. Two saves at once each keep
    the other's names (left_out_locked)."""
    with left_out_locked(data):
        known = read_left_out(data)
        for user_id, names in seen.items():
            known[user_id] = sorted(set(known.get(user_id, [])) | names)
        write_atomic(
            left_out_path(data),
            json.dumps(dict(sorted(known.items())), indent=2, ensure_ascii=False) + "\n",
        )


def opens_cleanly(tool: str, response) -> bool:
    """Whether a channel or thread read holds nothing before its first message but what the
    connector writes there: a channel read's "Channel: #name (C...)" line, a thread read
    nothing. An empty page is just that, so it passes."""
    text = result_text(response)
    markers = real_markers(text, tool == "slack_read_thread")
    before = (text[: markers[0].start()] if markers else text).strip()
    if tool == "slack_read_channel" and (header := CHANNEL_HEADER.match(before)):
        before = before[header.end() :].strip()
    return not before


def open_run(data: Path) -> Run | None:
    path = run_path(data)
    if not path.is_file():
        return None
    found = json.loads(path.read_text(encoding="utf-8"))
    found.pop("verified", None)  # the check mark before #21, now taken from the saved reads
    return Run(**found)


def close_run(data: Path, run: Run, why: str, now: datetime | None = None) -> None:
    """Move the open run to slack/runs/, so the hook stops saving."""
    run.closed_at = utc_iso(now or datetime.now(UTC))
    run.closed_why = why
    write_json(slack_dir(data) / "runs" / f"{run.run_id}.json", asdict(run))
    run_path(data).unlink(missing_ok=True)


def is_expired(run: Run, now: datetime) -> bool:
    return utc_iso(now) >= run.expires_at


def require_workspace(config: Config) -> None:
    if not config.slack_workspace_url:
        raise SlackExportError(
            "Add slack_workspace_url to [sources] in config.toml, such as "
            '"https://example.slack.com". Message links need it.'
        )


def run_channel_names(data: Path) -> dict[str, str]:
    """Channel ID to name, from every run so far. The newest name wins after a rename."""
    names: dict[str, str] = {}
    paths = sorted((slack_dir(data) / "runs").glob("*.json")) + [run_path(data)]
    for path in paths:
        if path.is_file():
            names.update(json.loads(path.read_text(encoding="utf-8"))["channels"])
    return names


def begin(config: Config, pairs: list[str], now: datetime | None = None, full: bool = False) -> str:
    """Open an export run for `pairs` like "example-channel=C00000001". Returns a report,
    which says where each channel's read and search start. A full run starts every channel
    from the beginning."""
    now = now or datetime.now(UTC)
    require_workspace(config)
    channels: dict[str, str] = {}
    for pair in pairs:
        name, sep, channel_id = pair.partition("=")
        name, channel_id = channel_name(name), channel_id.strip()
        if not sep or not name or not CHANNEL_ID.fullmatch(channel_id):
            raise SlackExportError(
                f"'{pair}' isn't a channel as name=ID, with an ID starting with C or G."
            )
        if name not in config.slack_channels:
            raise SlackExportError(
                f"#{name} isn't in slack_channels in config.toml. Listing a channel there "
                "is the opt-in to collecting it (CON-1)."
            )
        channels[channel_id] = name

    notes = []
    previous = open_run(config.data_dir)
    if previous is not None:
        close_run(config.data_dir, previous, "replaced by a new run", now)
        notes.append(f"Closed the earlier run {previous.run_id}, which was still open.")

    # A canvas is read if config.toml lists it, or its latest decision at review is "approved".
    approved = approvals.approved_canvases(config)
    canvases = list(config.slack_canvases)
    canvases += [canvas for canvas in approved or [] if canvas not in canvases]
    run = Run(
        run_id=now.strftime("%Y%m%dT%H%M%SZ"),
        started_at=utc_iso(now),
        expires_at=utc_iso(now + timedelta(minutes=RUN_MINUTES)),
        channels=channels,
        canvases=canvases,
        full=full,
    )
    write_json(run_path(config.data_dir), asdict(run))
    notes.append(
        f"Export run {run.run_id} is open for {len(channels)} channel(s) and "
        f"{len(run.canvases)} canvas(es). It expires at {run.expires_at[11:16]} UTC."
    )
    if approved is None:
        notes.append(
            "Couldn't read the approvals file next to config.toml, so this run reads only the "
            "canvases config.toml lists."
        )
    elif len(canvases) > len(config.slack_canvases):
        notes.append(
            f"{len(canvases) - len(config.slack_canvases)} of the canvases were approved at "
            "review, and the rest are listed in config.toml."
        )
    if full:
        notes.append("This is a full run: every channel from the start, and every listed canvas.")
    points = {} if full else slack_state.read_state(config.data_dir)
    if points is None:  # no state file yet, as after updating diffdata, or a damaged one
        points = slack_state.start_points(inbox_dir(config.data_dir))[0]
    notes.append("Where each channel starts:")
    for channel_id in channels:
        start = slack_state.planned_start(points.get(channel_id))
        if start is None:
            notes.append(f"{channel_id}: from the start")
        else:
            when = datetime.fromtimestamp(start, UTC)
            notes.append(f'{channel_id}: from "{start}" ({when:%Y-%m-%d %H:%M} UTC)')
    return "\n".join(notes)


def channel_checked(data: Path, run: Run, channel_id: str) -> bool:
    """Whether this channel's read has shown its opted-in name and ID in this run: the hook
    saves a channel read only after that check."""
    folder = inbox_dir(data) / channel_id / run.run_id
    return any(folder.glob("slack_read_channel-*.json"))


def search_channel(data: Path, run: Run, tool_input) -> str:
    """The channel a search is for, if the run allows it: it asks for files only or messages
    only, leaves out DMs and group DMs, its filter is exactly one channel the run names,
    whose read has passed its check, and nothing else in it could widen the search.
    Otherwise raises SlackExportError, saying what's wrong (CON-1)."""
    kind = tool_input.get("content_types") if isinstance(tool_input, dict) else None
    if kind not in SEARCH_KINDS:
        raise SlackExportError("This Slack search doesn't ask for files only or messages only")
    allowed = MESSAGE_SEARCH_FIELDS if kind == "messages" else SEARCH_FIELDS
    if tool_input.keys() - allowed or tool_input.get("natural_language_query", "") != "":
        raise SlackExportError(
            "This Slack search sets more than the channel filter /slack-export uses"
        )
    after = tool_input.get("after", "0")
    if not isinstance(after, str) or not UNIX_TIME.fullmatch(after):
        raise SlackExportError("This Slack search's start isn't a time as /slack-export gives it")
    # The tool leaves bots out unless told otherwise, and a bot's late reply counts too.
    if kind == "messages" and tool_input.get("include_bots") is not True:
        raise SlackExportError("This message search leaves out bots' replies")
    channel_types = tool_input.get("channel_types")
    if (
        not isinstance(channel_types, str)
        or {channel_type.strip() for channel_type in channel_types.split(",")}
        != SEARCH_CHANNEL_TYPES
    ):
        raise SlackExportError("This Slack search doesn't leave out DMs and group DMs")
    filters = tool_input.get("filters")
    scope = SEARCH_FILTER.fullmatch(filters.strip()) if isinstance(filters, str) else None
    if scope is None or scope["id"] not in run.channels:
        raise SlackExportError(
            "This Slack search isn't limited to exactly one channel the export run names"
        )
    if not channel_checked(data, run, scope["id"]):
        raise SlackExportError("This Slack search is for a channel that hasn't been read yet")
    return scope["id"]


def abort(config: Config, now: datetime | None = None) -> str:
    run = open_run(config.data_dir)
    if run is None:
        return "No export run is open."
    close_run(config.data_dir, run, "aborted", now)
    return f"Closed export run {run.run_id}. Results already saved stay in the inbox."


def save(config: Config, payload: bytes, now: datetime | None = None) -> int:
    """Save one hook payload: a channel or thread read to the inbox, a canvas read to its
    document folder, a files search's canvas sizes, and a message search's threads. Returns
    the hook's exit code.

    0 means saved, or nothing to save. 2 means a run is open but this result couldn't be
    saved; Claude Code shows our stderr message to Claude, so the export can stop.
    """
    now = now or datetime.now(UTC)
    run = open_run(config.data_dir)
    if run is None:
        return 0
    if is_expired(run, now):
        close_run(config.data_dir, run, "expired", now)
        return fail(
            f"The Slack export run {run.run_id} expired after {RUN_MINUTES} minutes, so this "
            "result wasn't saved. Start again with /slack-export."
        )
    try:
        hook = json.loads(payload.decode("utf-8"))
        tool = hook["tool_name"].rsplit("__", 1)[-1]
        # What was read: a canvas for a canvas read, else a channel. A search names its
        # channel in its filter, checked below.
        if tool != SEARCH_TOOL:
            read_id = hook["tool_input"]["canvas_id" if tool == slack_canvas.TOOL else "channel_id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return fail("The hook passed a Slack result diffdata can't read, so it wasn't saved.")
    if tool == SEARCH_TOOL:
        try:
            read_id = search_channel(config.data_dir, run, hook.get("tool_input"))
        except SlackExportError as e:
            return fail(f"{e}, so nothing from it was saved (CON-1). Stop the export.")
    elif tool == slack_canvas.TOOL:
        if read_id not in run.canvases:
            return fail(
                "This canvas isn't listed in slack_canvases in config.toml or approved at "
                "review, so it wasn't saved (CON-1). Stop the export."
            )
    elif tool not in SAVED_TOOLS or not isinstance(read_id, str) or read_id not in run.channels:
        return 0

    response = hook.get("tool_response")
    overflow = None
    if result_json(response) is None and (notice := OVERFLOW.search(raw_text(response))):
        overflow = overflow_file(notice["path"], hook.get("transcript_path"))
        if overflow is None:
            return fail(
                "This Slack result was too big to pass to the hook, and diffdata couldn't find "
                "the file Claude Code saved it in, so it wasn't saved. Stop the export."
            )
        try:
            response = overflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return fail(f"Couldn't read the file Claude Code saved a big Slack result in: {e}")

    # The call's ID keeps file names unique when Claude reads several things at once.
    call = re.sub(r"[^A-Za-z0-9_-]", "", str(hook.get("tool_use_id", "")))
    call = call or now.strftime("%Y%m%dT%H%M%S%fZ")
    if tool == SEARCH_TOOL and hook["tool_input"]["content_types"] == "messages":
        try:
            note = save_message_search(
                config.data_dir, run, read_id, hook, response, call, utc_iso(now)
            )
        except SlackExportError as e:
            return fail(str(e))
        return tell_claude(note)
    if tool == SEARCH_TOOL:
        try:
            note = slack_canvas.save_search(
                config.data_dir, run.run_id, read_id, run.canvases, response, call, utc_iso(now)
            )
        except slack_canvas.CanvasError as e:
            return fail(str(e))
        return tell_claude(note)
    left, left_names = 0, {}
    if tool in SAVED_TOOLS and config.slack_opted_out:
        # People who opted out (#105): their messages are cut to a stub before anything is
        # written, keeping only what finds their threads and checks the pages.
        opted_out = set(config.slack_opted_out)
        shaped = result_json(response)
        if not (isinstance(shaped, dict) and isinstance(shaped.get("messages"), str)) and any(
            user_id in json.dumps(response) for user_id in opted_out
        ):
            # Not the connector's usual shape, so the filter can't find the messages to cut,
            # and someone who opted out is in it somewhere. A result in this shape never
            # counts towards the start points anyway (slack_state.a_read).
            return fail(
                "This Slack result isn't in the shape diffdata expects, and it includes "
                "someone in slack_opted_out, so nothing was saved. Stop the export and tell "
                "the user: they can pass this message to the project team."
            )
        before, skipped_before = parse_result(hook["tool_name"], hook["tool_input"], response)
        if not opens_cleanly(tool, response):
            # Before the first message, or on a page where the parser finds none, there's more
            # than the channel line, such as an author line that shows no ID. The filter can't
            # tell whose words those are, so they're never saved while anyone is opted out.
            return fail(
                "This Slack result holds text diffdata couldn't split into messages, and "
                "someone is in slack_opted_out, so nothing was saved. Stop the export and tell "
                "the user: they can pass this message to the project team, with any names or "
                "text replaced, so the filter can be fixed."
            )
        response, left_names, theirs_unreadable = leave_out(response, opted_out, hook["tool_name"])
        after, skipped_after = parse_result(hook["tool_name"], hook["tool_input"], response)
        others = [{m.ts for m in found if m.user_id not in opted_out} for found in (before, after)]
        if others[0] != others[1] or skipped_before - theirs_unreadable != skipped_after:
            # The cut must leave everyone else's messages, and the count of messages diffdata
            # can't read, exactly as they were: a reply lost, or an unreadable message hidden
            # from the start-point checks, would be worse than stopping.
            return fail(
                "Cutting out someone in slack_opted_out would also have changed other people's "
                "messages in this Slack result, or someone pasted Slack's own header lines into "
                "a message, so nothing was saved. Stop the export and tell the user: they can "
                "pass this message to the project team, so the filter can be fixed."
            )
        if still_shows(response, opted_out) or id_left(response, opted_out):
            return fail(
                "This Slack result came in a shape diffdata couldn't cut someone in "
                "slack_opted_out out of, or someone pasted a line naming them as an author, so "
                "nothing was saved. Stop the export and tell the "
                "user: they can pass this message to the project team, with any names or text "
                "replaced, so the filter can be fixed."
            )
        left = sum(m.user_name == OPTED_OUT_NAME for m in after)
    record = {
        "saved_at": utc_iso(now),
        "run_id": run.run_id,
        "tool_name": hook["tool_name"],
        "tool_input": hook["tool_input"],
        "tool_response": response,
    }
    if overflow is not None:
        record["overflow_file"] = overflow.name
    if left:
        record["left_out"] = left  # a count, never who
    if tool == slack_canvas.TOOL:
        # To the microsecond, so two reads in the same second keep their order (COL-9).
        record["saved_at"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            return tell_claude(
                slack_canvas.save_read(config.data_dir, run.run_id, read_id, record, call)
            )
        except slack_canvas.CanvasError as e:
            return fail(str(e))

    channel_id = read_id
    if tool == "slack_read_channel":
        header = CHANNEL_HEADER.match(result_text(response).lstrip())
        if (
            header is None
            or header["id"] != channel_id
            or channel_name(header["name"]) != run.channels[channel_id]
        ):
            return fail(
                f"This channel read doesn't show the channel opted in as "
                f"#{run.channels[channel_id]} in config.toml, so it wasn't saved (CON-1). "
                "Stop the export."
            )
    elif not channel_checked(config.data_dir, run, channel_id):
        return fail(
            f"A thread in #{run.channels[channel_id]} was read before the channel itself, so "
            "diffdata couldn't check the channel and didn't save it. Stop the export."
        )

    path = inbox_dir(config.data_dir) / channel_id / run.run_id / f"{tool}-{call}.json"
    if left_names:  # only once the read has passed its checks and is about to be saved
        try:
            record_left_out(config.data_dir, left_names)
        except (OSError, SlackExportError) as e:
            return fail(f"Couldn't record who was left out, so nothing was saved: {e}")
    try:
        write_json(path, record)
    except OSError as e:
        return fail(f"Couldn't save a Slack result to the inbox: {e}")
    note = saved_note(tool, hook["tool_name"], hook["tool_input"], response, overflow is not None)
    if left:
        note += f" {left} message(s) by people in slack_opted_out were left out of what was saved."
    return tell_claude(note)


def check(config: Config, payload: bytes, now: datetime | None = None) -> int:
    """Before any Slack tool: let it go ahead only if it finds a channel's ID, or the open
    run names the channel or canvas it reads, or the search is one `search_channel()` allows
    (CON-1). Any other Slack tool is stopped. Returns the hook's exit code: 0 lets the call
    go ahead, and 2 stops it before anything is fetched, with our stderr message shown to
    Claude. Writes nothing."""
    now = now or datetime.now(UTC)
    try:
        hook = json.loads(payload.decode("utf-8"))
        tool = hook["tool_name"].rsplit("__", 1)[-1]
        if tool == FIND_CHANNEL:
            return 0
        if tool != SEARCH_TOOL and tool not in CHECKED_READS:
            return fail(
                f"/slack-export never uses {tool}, so it was stopped before it ran. "
                "Stop the export."
            )
        if tool != SEARCH_TOOL:
            field_name = CHECKED_READS[tool]
            read_id = hook["tool_input"][field_name]
            # Concise results have no message headers, so nothing in them could be cut for
            # people who opted out (#105). Detailed is the default, and what the export asks.
            if tool in SAVED_TOOLS and hook["tool_input"].get("response_format", "detailed") != (
                "detailed"
            ):
                return fail(
                    "/slack-export reads channels and threads with response_format detailed "
                    "only, so this read was stopped before it ran. Call it again with "
                    'response_format "detailed".'
                )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return fail(
            "Couldn't tell what this Slack read is for, so it was stopped. Stop the export."
        )
    run = open_run(config.data_dir)
    if run is None or is_expired(run, now):
        return fail(
            "No export run is open, so this Slack read was stopped. Stop, and tell the user: to "
            "export, run /slack-export again. To read Slack for anything else, use a new Claude "
            "session, because this check stays on for the rest of a session that ran /slack-export."
        )
    if tool == SEARCH_TOOL:
        try:
            search_channel(config.data_dir, run, hook.get("tool_input"))
        except SlackExportError as e:
            return fail(f"{e}, so it was stopped before it ran (CON-1). Stop the export.")
        return 0
    named = run.canvases if field_name == "canvas_id" else run.channels
    if not isinstance(read_id, str) or read_id not in named:
        what = "canvas" if field_name == "canvas_id" else "channel"
        return fail(
            f"This {what} isn't one the export run took from config.toml or the approvals, so "
            "the read was stopped before it ran (CON-1). Stop the export."
        )
    return 0


def tell_claude(note: str) -> int:
    """Pass the hook's note to Claude, after a save."""
    hook_output = {"hookEventName": "PostToolUse", "additionalContext": note}
    print(json.dumps({"hookSpecificOutput": hook_output}))
    return 0


def overflow_file(notice_path: str, transcript_path) -> Path | None:
    """The file Claude Code saved a too-big result in, if it's in this session's own
    tool-results folder. Anything else is refused, so the hook never reads other files."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    folder = (Path(transcript_path).with_suffix("") / "tool-results").resolve()
    path = Path(notice_path.strip().strip("'\"")).resolve()
    return path if path.is_relative_to(folder) and path.is_file() else None


def saved_note(tool: str, tool_name: str, tool_input: dict, response, overflowed: bool) -> str:
    """What Claude needs to carry on: a big result shows Claude only a preview, which may
    not reach the next cursor or every post with replies. IDs and counts only."""
    messages, _ = parse_result(tool_name, tool_input, response)
    note = f"diffdata saved this result to the inbox: {len(messages)} message(s)."
    if overflowed:
        note += " It was too big to show you in full, so diffdata read Claude Code's saved copy."
    if not messages and tool == "slack_read_channel" and "oldest" in tool_input:
        note += " That's normal when nothing was posted since the start point."
    elif not messages:
        note += " That's unexpected: if you expected messages, stop and tell the user."
    cursor = next_cursor(response)
    if cursor:
        note += f" Next page: call again with cursor `{cursor}`."
    elif pages_known(response):
        note += " No more pages."
    else:
        note += " diffdata can't tell whether there are more pages: stop and tell the user."
    if tool == "slack_read_channel":
        threads = [m.ts for m in messages if m.replies]
        note += (
            " Posts with replies, to read with slack_read_thread: " + ", ".join(threads) + "."
            if threads
            else " No posts with replies on this page."
        )
    return note


def save_message_search(
    data: Path, run: Run, channel_id: str, hook: dict, response, call: str, saved_at: str
) -> str:
    """Save what slack_state.py needs from a message search: its channel, start and cursors,
    and the threads it found replies in. No message text, names or emails are kept: the
    thread reads bring the same messages anyway. Refused, with nothing saved, if a result is
    from another channel (CON-1). Returns the note for Claude, which gives counts and IDs."""
    results = search_messages(response)
    if results is None:
        raise SlackExportError(
            "This message search shows a message without a channel, time or link, or diffdata "
            "can't find its results, so nothing was saved. The connector's format may have "
            "changed. Stop the export."
        )
    if any(channel != channel_id for channel, _, _ in results):
        raise SlackExportError(
            "This message search shows a message from a channel the export run doesn't name, so "
            "nothing from it was saved (CON-1). Stop the export."
        )
    threads = sorted({thread for _, _, thread in results if thread})
    tool_input = hook["tool_input"]
    record = {
        "saved_at": saved_at,
        "run_id": run.run_id,
        "tool_name": hook["tool_name"],
        "channel_id": channel_id,
        "after": tool_input.get("after"),
        "cursor": tool_input.get("cursor"),
        "next_cursor": next_cursor(response),
        "threads": threads,
    }
    path = inbox_dir(data) / channel_id / run.run_id / f"search_messages-{call}.json"
    try:
        write_json(path, record)
    except OSError as e:
        raise SlackExportError(f"Couldn't save a message search to the inbox: {e}") from e
    note = f"diffdata saved this search: {len(results)} message(s), "
    note += (
        f"with new replies in {len(threads)} thread(s), to read with slack_read_thread: "
        + ", ".join(threads)
        + "."
        if threads
        else "with no thread replies on this page."
    )
    cursor = record["next_cursor"]
    return note + (
        f" Next page: call again with cursor `{cursor}`." if cursor else " No more pages."
    )


def fail(message: str) -> int:
    print(f"diffdata: {message}", file=sys.stderr)
    return 2


def check_from_hook(payload: bytes) -> int:
    """Entry point for the before-read hook. Claude Code stops a call when the hook exits
    with code 2. The command's onFailure: block also stops it when the hook can't start or
    times out, but an exit code we choose gives Claude a clearer reason. So anything that
    goes wrong here, such as an unreadable config.toml or a damaged run.json, stops the
    read with a message."""
    try:
        return check(load_config(), payload)
    except (ConfigError, UnsafeDataDirError):
        return fail("Couldn't read config.toml, so this Slack read was stopped. Stop the export.")
    except Exception as e:
        return fail(
            f"Couldn't check this Slack read ({type(e).__name__}), so it was stopped. "
            "Stop the export."
        )


def save_from_hook(payload: bytes) -> int:
    """Entry point for the hook. Without a usable config no run can be open, so do nothing."""
    try:
        config = load_config()
    except (ConfigError, UnsafeDataDirError):
        return 0
    return save(config, payload)
