"""The Slack inbox: raw connector results, saved unchanged, one file per tool call (DIS-1).

For the pilot there's no Slack app (DIS-6). The /slack-export command reads Slack through
Claude's Slack connector, and a hook passes each result to `save_from_hook()`, so Claude
never retypes it. A result is saved only while an export run is open, and only for the
channels that run named, all of which must be opted in through config.toml (CON-1).
A result too big for Claude's context reaches the hook only as a notice naming the file
Claude Code saved it in, so `save` reads that file, and only from the session's own
tool-results folder. After each save, the hook tells Claude what it needs to carry on.

Layout under the data folder:
  slack/run.json                                   the open run, if any
  slack/runs/<run-id>.json                         every closed run, and why it closed
  slack/inbox/<channel-id>/<run-id>/<tool>-<call>.json   one raw tool result
"""

import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.common.config import Config, ConfigError, channel_name, load_config
from diffdata.common.paths import UnsafeDataDirError
from diffdata.discover.slack_parse import next_cursor, parse_result, raw_text, result_json

# A run that crashed or was forgotten must not leave the hook saving, so runs expire.
RUN_MINUTES = 60
# The connector tools whose results we save. The command's hook matches the same two.
SAVED_TOOLS = ("slack_read_channel", "slack_read_thread")
# When a result is too big for Claude's context, Claude Code saves it to a file in the
# session's tool-results folder, and the hook gets only a notice naming that file.
OVERFLOW = re.compile(r"saved to:?\s+(?P<path>.+?\.(?:txt|json))\b", re.IGNORECASE)
# Public channels start with C, older private ones with G. DMs (D) need both people's
# consent first (CON-5), so they're refused.
CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{6,}")


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
    return Run(**json.loads(path.read_text(encoding="utf-8")))


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


def begin(config: Config, pairs: list[str], now: datetime | None = None) -> str:
    """Open an export run for `pairs` like "example-channel=C00000001". Returns a report."""
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
    )
    write_json(run_path(config.data_dir), asdict(run))
    notes.append(
        f"Export run {run.run_id} is open for {len(channels)} channel(s). "
        f"It expires at {run.expires_at[11:16]} UTC."
    )
    return "\n".join(notes)


def abort(config: Config, now: datetime | None = None) -> str:
    run = open_run(config.data_dir)
    if run is None:
        return "No export run is open."
    close_run(config.data_dir, run, "aborted", now)
    return f"Closed export run {run.run_id}. Results already saved stay in the inbox."


def save(config: Config, payload: bytes, now: datetime | None = None) -> int:
    """Save one hook payload to the inbox. Returns the hook's exit code.

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
        channel_id = hook["tool_input"]["channel_id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return fail("The hook passed a Slack result diffdata can't read, so it wasn't saved.")
    if tool not in SAVED_TOOLS or not isinstance(channel_id, str) or channel_id not in run.channels:
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
    path = inbox_dir(config.data_dir) / channel_id / run.run_id / f"{tool}-{call}.json"
    record = {
        "saved_at": utc_iso(now),
        "run_id": run.run_id,
        "tool_name": hook["tool_name"],
        "tool_input": hook["tool_input"],
        "tool_response": response,
    }
    if overflow is not None:
        record["overflow_file"] = overflow.name
    try:
        write_json(path, record)
    except OSError as e:
        return fail(f"Couldn't save a Slack result to the inbox: {e}")

    note = saved_note(tool, hook["tool_name"], hook["tool_input"], response, overflow is not None)
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
    if not messages:
        note += " That's unexpected: if you expected messages, stop and tell the user."
    cursor = next_cursor(response)
    note += f" Next page: call again with cursor `{cursor}`." if cursor else " No more pages."
    if tool == "slack_read_channel":
        threads = [m.ts for m in messages if m.replies]
        note += (
            " Posts with replies, to read with slack_read_thread: " + ", ".join(threads) + "."
            if threads
            else " No posts with replies on this page."
        )
    return note


def fail(message: str) -> int:
    print(f"diffdata: {message}", file=sys.stderr)
    return 2


def save_from_hook(payload: bytes) -> int:
    """Entry point for the hook. Without a usable config no run can be open, so do nothing."""
    try:
        config = load_config()
    except (ConfigError, UnsafeDataDirError):
        return 0
    return save(config, payload)
