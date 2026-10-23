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
Canvas reads go to collect/slack_canvas.py (COL-2), and only for the canvases listed in
config.toml, which every run names.
A search must ask for files only or messages only, leave out DMs, and name one channel the
run names, after that channel's read has passed its check. A files search goes to the
canvas code, which keeps only the listed canvases' sizes. A message search, which finds
new replies in older threads, is saved as its channel, start, cursors and the threads it
found replies in, never its text, and is refused if any result is from another channel.
A second hook runs `check()` before each read or search, so one the run doesn't name is
stopped before anything is fetched. Both hooks set onFailure: block, so a hook that can't
start or times out stops the read too.
`begin` says where each channel's read and search start, from slack_state.py.

Layout under the data folder:
  slack/run.json                                   the open run, if any
  slack/runs/<run-id>.json                         every closed run, and why it closed
  slack/state.json                                 each channel's start point (slack_state.py)
  slack/inbox/<channel-id>/<run-id>/<tool>-<call>.json   one raw channel or thread read
  slack/inbox/<channel-id>/<run-id>/search_messages-<call>.json   one message search's threads
"""

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.collect import slack_canvas
from diffdata.common.config import Config, ConfigError, channel_name, load_config
from diffdata.common.paths import UnsafeDataDirError
from diffdata.discover import slack_state
from diffdata.discover.slack_parse import (
    next_cursor,
    pages_known,
    parse_result,
    raw_text,
    result_json,
    result_text,
    search_messages,
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
    # The canvases listed in config.toml when the run opened. Only these reads are saved.
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

    run = Run(
        run_id=now.strftime("%Y%m%dT%H%M%SZ"),
        started_at=utc_iso(now),
        expires_at=utc_iso(now + timedelta(minutes=RUN_MINUTES)),
        channels=channels,
        canvases=list(config.slack_canvases),
        full=full,
    )
    write_json(run_path(config.data_dir), asdict(run))
    notes.append(
        f"Export run {run.run_id} is open for {len(channels)} channel(s) and "
        f"{len(run.canvases)} canvas(es). It expires at {run.expires_at[11:16]} UTC."
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
                "This canvas isn't listed in slack_canvases in config.toml, so it wasn't saved "
                "(CON-1). Stop the export."
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
    record = {
        "saved_at": utc_iso(now),
        "run_id": run.run_id,
        "tool_name": hook["tool_name"],
        "tool_input": hook["tool_input"],
        "tool_response": response,
    }
    if overflow is not None:
        record["overflow_file"] = overflow.name
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
    try:
        write_json(path, record)
    except OSError as e:
        return fail(f"Couldn't save a Slack result to the inbox: {e}")
    return tell_claude(
        saved_note(tool, hook["tool_name"], hook["tool_input"], response, overflow is not None)
    )


def check(config: Config, payload: bytes, now: datetime | None = None) -> int:
    """Before a Slack read or search: let it go ahead only if the open run names the channel
    or canvas it reads, or the search is one `search_channel()` allows (CON-1). Returns the
    hook's exit code: 0 lets the call go ahead, and 2 stops it before anything is fetched,
    with our stderr message shown to Claude. Writes nothing."""
    now = now or datetime.now(UTC)
    try:
        hook = json.loads(payload.decode("utf-8"))
        tool = hook["tool_name"].rsplit("__", 1)[-1]
        if tool != SEARCH_TOOL:
            field_name = CHECKED_READS[tool]
            read_id = hook["tool_input"][field_name]
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
            f"This {what} isn't one the export run took from config.toml, so the read was "
            "stopped before it ran (CON-1). Stop the export."
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
