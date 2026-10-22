"""The Slack inbox: export runs, and the hook that saves raw results (DIS-1). Synthetic data."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common.config import Config
from diffdata.discover import slack_inbox
from diffdata.discover.slack_inbox import SlackExportError, abort, begin, open_run, save

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
CHANNEL = "C00000001"
FIXTURES = Path(__file__).parent / "fixtures" / "slack"


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
    )


# The smallest channel read: just its header line, in the connector's wrapping.
HEADER_ONLY = [
    {"type": "text", "text": json.dumps({"messages": "Channel: #example-channel (C00000001)\n"})}
]


def payload(tool="slack_read_channel", channel=CHANNEL, call="toolu_0001", response=None):
    return json.dumps(
        {
            "session_id": "fake-session",
            "transcript_path": "/fake/transcript.jsonl",
            "hook_event_name": "PostToolUse",
            "tool_name": f"mcp__claude_ai_Slack__{tool}",
            "tool_input": {"channel_id": channel, "response_format": "detailed"},
            "tool_response": HEADER_ONLY if response is None else response,
            "tool_use_id": call,
        }
    ).encode("utf-8")


def inbox_files(config):
    return sorted(slack_inbox.inbox_dir(config.data_dir).rglob("*.json"))


def test_begin_opens_a_run_that_expires(config):
    begin(config, ["#Example-Channel=C00000001"], now=T0)
    run = open_run(config.data_dir)
    assert run.channels == {CHANNEL: "example-channel"}
    assert run.expires_at == "2026-10-01T15:00:00Z"


def test_begin_refuses_a_channel_that_isnt_opted_in(config):
    with pytest.raises(SlackExportError, match="opt-in"):
        begin(config, ["other-channel=C00000002"], now=T0)
    assert open_run(config.data_dir) is None


@pytest.mark.parametrize("pair", ["example-channel", "example-channel=D00000001", "=C00000001"])
def test_begin_refuses_a_malformed_channel_or_a_dm(config, pair):
    with pytest.raises(SlackExportError):
        begin(config, [pair], now=T0)


def test_begin_needs_the_workspace_address(tmp_path):
    config = Config(data_dir=tmp_path / "data", slack_channels=("example-channel",))
    with pytest.raises(SlackExportError, match="slack_workspace_url"):
        begin(config, ["example-channel=C00000001"], now=T0)


def test_begin_closes_a_run_left_open(config):
    begin(config, ["example-channel=C00000001"], now=T0)
    first = open_run(config.data_dir).run_id
    report = begin(config, ["example-channel=C00000001"], now=T0 + timedelta(minutes=5))
    assert first in report
    closed = json.loads((config.data_dir / "slack" / "runs" / f"{first}.json").read_text())
    assert closed["closed_why"] == "replaced by a new run"


def test_save_does_nothing_without_an_open_run(config):
    assert save(config, payload(), now=T0) == 0
    assert inbox_files(config) == []


def test_save_keeps_the_result_unchanged_and_drops_session_details(config):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, payload(), now=T0 + timedelta(minutes=1)) == 0
    [path] = inbox_files(config)
    assert path.parent.parent.name == CHANNEL
    assert path.name == "slack_read_channel-toolu_0001.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tool_response"] == HEADER_ONLY
    assert saved["tool_input"]["channel_id"] == CHANNEL
    assert saved["saved_at"] == "2026-10-01T14:01:00Z"
    assert "session_id" not in saved and "transcript_path" not in saved


@pytest.mark.parametrize(
    "other",
    [
        payload(channel="C00000002"),  # a channel this run didn't name
        payload(tool="slack_read_file"),  # not a tool we save; a search is checked instead
    ],
)
def test_save_ignores_other_channels_and_tools(config, other):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, other, now=T0) == 0
    assert inbox_files(config) == []


def test_an_expired_run_stops_saving_and_tells_claude(config, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, payload(), now=T0 + timedelta(minutes=61)) == 2
    assert "expired" in capsys.readouterr().err
    assert open_run(config.data_dir) is None
    assert inbox_files(config) == []
    assert save(config, payload(), now=T0 + timedelta(minutes=62)) == 0


def test_a_result_without_a_call_id_still_gets_a_safe_file_name(config):
    begin(config, ["example-channel=C00000001"], now=T0)
    hook = json.loads(payload())
    del hook["tool_use_id"]
    assert save(config, json.dumps(hook).encode("utf-8"), now=T0) == 0
    [path] = inbox_files(config)
    assert path.name == "slack_read_channel-20261001T140000000000Z.json"


def test_an_unreadable_payload_during_a_run_tells_claude(config, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, b"not json", now=T0) == 2
    assert "can't read" in capsys.readouterr().err


def test_abort_closes_the_run(config):
    assert abort(config) == "No export run is open."
    begin(config, ["example-channel=C00000001"], now=T0)
    abort(config, now=T0)
    assert open_run(config.data_dir) is None


def test_the_hook_does_nothing_without_a_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert slack_inbox.save_from_hook(payload()) == 0


MORE = "There are more messages available. To view the next page, use cursor: `FAKECURSOR01`\n"


def connector_result(fixture: str, pagination: str = MORE) -> list[dict]:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    return [{"type": "text", "text": json.dumps({"messages": text, "pagination_info": pagination})}]


def hook_note(capsys) -> str:
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    return output["additionalContext"]


@pytest.mark.parametrize(
    "header",
    [
        "Channel: #other-channel (C00000001)\n",  # another channel's name
        "Channel: #example-channel (C00000009)\n",  # another channel's ID
        "no header at all\n",
    ],
)
def test_a_channel_read_that_isnt_the_opted_in_channel_is_refused(config, capsys, header):
    begin(config, ["example-channel=C00000001"], now=T0)
    response = [{"type": "text", "text": json.dumps({"messages": header})}]
    assert save(config, payload(response=response), now=T0) == 2
    assert "CON-1" in capsys.readouterr().err
    assert inbox_files(config) == []


def test_a_thread_is_saved_only_after_its_channel_passed_the_check(config, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    thread = payload(tool="slack_read_thread", call="toolu_0002")
    assert save(config, thread, now=T0) == 2
    assert "before the channel" in capsys.readouterr().err
    assert inbox_files(config) == []

    assert save(config, payload(), now=T0) == 0
    assert slack_inbox.channel_checked(config.data_dir, open_run(config.data_dir), CHANNEL)
    assert save(config, thread, now=T0) == 0
    assert len(inbox_files(config)) == 2


def test_save_tells_claude_the_next_cursor_and_the_threads_to_read(config, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    hook = json.loads(payload())
    hook["tool_response"] = connector_result("read_channel.txt")
    assert save(config, json.dumps(hook).encode("utf-8"), now=T0) == 0
    note = hook_note(capsys)
    assert note.startswith("diffdata saved this result to the inbox: 5 message(s).")
    assert "cursor `FAKECURSOR01`" in note
    assert "slack_read_thread: 1790864040.000100." in note
    assert "Alex" not in note and "Kickoff" not in note  # IDs and counts only


@pytest.fixture
def overflowed(tmp_path):
    """Claude Code's layout: the session's transcript, and beside it a folder of the same
    name whose tool-results folder holds results too big for Claude's context."""
    transcript = tmp_path / "claude" / "projects" / "repo" / "session-0001.jsonl"
    folder = transcript.with_suffix("") / "tool-results"
    folder.mkdir(parents=True)

    def make(saved_text: str, notice_path=None) -> bytes:
        saved = folder / "toolu_fake0001.txt"
        saved.write_text(saved_text, encoding="utf-8")
        notice = (
            "Output too large (80.4KB). Full output saved to: "
            f"{notice_path or saved}\n\nPreview (first 2KB):\n{saved_text[:200]}"
        )
        hook = json.loads(payload())
        hook["transcript_path"] = str(transcript)
        hook["tool_response"] = [{"type": "text", "text": notice}]
        return json.dumps(hook).encode("utf-8")

    return make


@pytest.mark.parametrize("as_blocks", [False, True])
def test_a_result_too_big_for_claude_is_read_from_claude_codes_saved_copy(
    config, capsys, overflowed, as_blocks
):
    response = connector_result("read_channel.txt", pagination="")
    saved_text = json.dumps(response) if as_blocks else response[0]["text"]
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, overflowed(saved_text), now=T0) == 0
    [path] = inbox_files(config)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tool_response"] == saved_text
    assert saved["overflow_file"] == "toolu_fake0001.txt"
    note = hook_note(capsys)
    assert "5 message(s)" in note and "too big" in note and "No more pages" in note


def test_a_saved_copy_outside_the_sessions_folder_is_refused(config, capsys, overflowed, tmp_path):
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("{}", encoding="utf-8")
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, overflowed("{}", notice_path=elsewhere), now=T0) == 2
    assert "Stop the export" in capsys.readouterr().err
    assert inbox_files(config) == []


def test_a_missing_saved_copy_stops_the_export(config, capsys, overflowed, tmp_path):
    missing = tmp_path / "claude" / "projects" / "repo" / "session-0001" / "tool-results" / "x.txt"
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, overflowed("{}", notice_path=missing), now=T0) == 2
    assert "Stop the export" in capsys.readouterr().err
    assert inbox_files(config) == []


def test_cli_begin_and_abort(tmp_path, monkeypatch, capsys):
    data = (tmp_path / "data").as_posix()
    (tmp_path / "config.toml").write_text(
        '[sources]\nslack_channels = ["example-channel"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f'[local]\ndata_dir = "{data}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["discover", "slack", "begin", "example-channel=C00000001"]) == 0
    assert cli.main(["discover", "slack", "begin", "other=C00000002"]) == 1
    assert "opt-in" in capsys.readouterr().err
    assert cli.main(["discover", "slack", "abort"]) == 0
    assert cli.main(["discover", "slack", "begin", "--full", "example-channel=C00000001"]) == 0
    assert open_run(tmp_path / "data").full


# The check before each read (CON-1): a read the run doesn't name is stopped before it runs.

CANVAS = "F00000003"
THREAD_TS = "1790864040.000100"


@pytest.fixture
def run_open(config):
    config = replace(config, slack_canvases=(CANVAS,))
    begin(config, ["example-channel=C00000001"], now=T0)
    return config


@pytest.mark.parametrize(
    "tool, tool_input",
    [
        ("slack_read_channel", {"channel_id": CHANNEL}),
        ("slack_read_thread", {"channel_id": CHANNEL, "message_ts": THREAD_TS}),
        ("slack_read_canvas", {"canvas_id": CANVAS}),
    ],
)
def test_check_lets_a_read_the_run_names_go_ahead(run_open, slack_hook, tool, tool_input):
    before = sorted(run_open.data_dir.rglob("*"))
    assert slack_inbox.check(run_open, slack_hook(tool, tool_input, None), now=T0) == 0
    assert sorted(run_open.data_dir.rglob("*")) == before  # it writes nothing


@pytest.mark.parametrize(
    "tool, tool_input",
    [
        ("slack_read_channel", {"channel_id": "C00000002"}),
        ("slack_read_thread", {"channel_id": "C00000002", "message_ts": THREAD_TS}),
        ("slack_read_canvas", {"canvas_id": "F00000004"}),
        ("slack_read_canvas", {"canvas_id": CHANNEL}),  # a channel the run names isn't a canvas
        ("slack_read_canvas", {"canvas_id": [CANVAS]}),  # not an ID
        ("slack_read_canvas", {"channel_id": CHANNEL}),  # names no canvas
        ("slack_read_file", {"file_id": CANVAS}),  # not a read the check knows
    ],
)
def test_check_stops_a_read_the_run_doesnt_name(run_open, slack_hook, capsys, tool, tool_input):
    assert slack_inbox.check(run_open, slack_hook(tool, tool_input, None), now=T0) == 2
    err = capsys.readouterr().err
    assert "stopped" in err and "Stop the export" in err
    assert "diffdata: diffdata" not in err


def test_check_stops_every_read_when_no_run_is_open(config, slack_hook, capsys):
    read = slack_hook("slack_read_channel", {"channel_id": CHANNEL}, None)
    assert slack_inbox.check(config, read, now=T0) == 2
    begin(config, ["example-channel=C00000001"], now=T0)
    assert slack_inbox.check(config, read, now=T0 + timedelta(minutes=61)) == 2  # expired
    err = capsys.readouterr().err
    assert err.count("No export run is open") == 2
    assert "new Claude session" in err  # the check stays on after /slack-export


def test_check_stops_the_read_without_a_config(slack_hook, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    read = slack_hook("slack_read_channel", {"channel_id": CHANNEL}, None)
    assert slack_inbox.check_from_hook(read) == 2
    err = capsys.readouterr().err
    assert "Stop the export" in err and "diffdata: diffdata" not in err


def test_cli_check_passes_the_hook_payload_to_the_check(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"fake": 1}')))
    monkeypatch.setattr(slack_inbox, "check_from_hook", lambda p: 2 if p == b'{"fake": 1}' else 0)
    assert cli.main(["discover", "slack", "check"]) == 2


SEARCH_TOOL = "slack_search_public_and_private"
FILES_SEARCH = {
    "content_types": "files",
    "filters": "in:<#C00000001>",
    "channel_types": "public_channel,private_channel",
}
# Searches the run doesn't allow, as changes to FILES_SEARCH (None leaves the field out).
BAD_SEARCHES = [
    {"content_types": "files,messages"},  # one kind at a time
    {"content_types": "messages,files"},
    {"content_types": ["files"]},
    {"content_types": None},  # the tool's default isn't documented
    {"after": "1790860440"},  # a message search's field, in a files search
    {"channel_types": None},  # the tool's default searches DMs too
    {"channel_types": "public_channel,private_channel,im"},
    {"filters": None},  # every file the person can see
    {"filters": "in:<#C00000002>"},  # a channel the run doesn't name
    {"filters": "in:<#C00000001> in:<#C00000002>"},
    {"filters": "in:<#C00000001> from:<@U00000001>"},  # anything more than the channel
    {"filters": "in:@alex"},
    # Keywords or a query could carry a filter of their own.
    {"natural_language_query": "in:<#C00000002>"},
    {"keywords": ["in:<#C00000002>"]},
    {"context_channel_id": "C00000002"},  # any field the command doesn't send
]


def search_input(change: dict) -> dict:
    return {k: v for k, v in (FILES_SEARCH | change).items() if v is not None}


@pytest.mark.parametrize(
    "tool_input",
    [
        FILES_SEARCH,
        # Everything /slack-export's step 5 sends, on a later page.
        FILES_SEARCH
        | {
            "natural_language_query": "",
            "response_format": "detailed",
            "include_context": False,
            "limit": 20,
            "cursor": "FAKECURSOR01",
        },
    ],
)
def test_check_lets_a_files_search_of_a_checked_channel_go_ahead(run_open, slack_hook, tool_input):
    assert save(run_open, payload(), now=T0) == 0  # the channel read passes its check
    search = slack_hook(SEARCH_TOOL, tool_input, None)
    assert slack_inbox.check(run_open, search, now=T0) == 0


@pytest.mark.parametrize("change", BAD_SEARCHES)
def test_check_stops_a_search_the_run_doesnt_allow(run_open, slack_hook, capsys, change):
    assert save(run_open, payload(), now=T0) == 0
    search = slack_hook(SEARCH_TOOL, search_input(change), None)
    assert slack_inbox.check(run_open, search, now=T0) == 2
    err = capsys.readouterr().err
    assert "so it was stopped before it ran (CON-1). Stop the export." in err


def test_check_stops_a_search_before_its_channel_is_read(run_open, slack_hook, capsys):
    assert slack_inbox.check(run_open, slack_hook(SEARCH_TOOL, FILES_SEARCH, None), now=T0) == 2
    assert "a channel that hasn't been read yet" in capsys.readouterr().err


@pytest.mark.parametrize("change", [*BAD_SEARCHES, "unread channel"])
def test_save_refuses_a_search_the_run_doesnt_allow(run_open, slack_hook, capsys, change):
    # The check after the search repeats the check before it, so nothing is saved either way.
    if change != "unread channel":
        assert save(run_open, payload(), now=T0) == 0
    tool_input = FILES_SEARCH if change == "unread channel" else search_input(change)
    response = [{"type": "text", "text": json.dumps({"results": "## Files (0 results)\n"})}]
    assert save(run_open, slack_hook(SEARCH_TOOL, tool_input, response), now=T0) == 2
    assert "nothing from it was saved (CON-1). Stop the export." in capsys.readouterr().err
    assert not (run_open.data_dir / "slack" / "canvas-sizes").exists()


# Message searches (#14), which find new replies in older threads.

MESSAGE_SEARCH = FILES_SEARCH | {
    "content_types": "messages",
    "after": "1790860440",
    "natural_language_query": "",
    "response_format": "detailed",
    "include_bots": True,
    "include_context": False,
    "sort": "timestamp",
    "limit": 20,
}
# Message searches the run doesn't allow, as changes to MESSAGE_SEARCH.
BAD_MESSAGE_SEARCHES = [
    {"filters": "in:<#C00000002>"},
    {"filters": "in:<#C00000001> is:thread"},
    {"channel_types": "public_channel,private_channel,mpim,im"},
    {"keywords": ["in:<#C00000002>"]},
    {"before": "1790960440"},  # any field the command doesn't send
    {"sort_dir": "asc"},
    {"after": 1790860440},  # the tool takes a string
    {"after": "2026-10-01"},
]


@pytest.mark.parametrize(
    "tool_input", [MESSAGE_SEARCH, MESSAGE_SEARCH | {"cursor": "FAKECURSOR02"}]
)
def test_check_lets_a_message_search_of_a_checked_channel_go_ahead(
    run_open, slack_hook, tool_input
):
    assert save(run_open, payload(), now=T0) == 0
    assert slack_inbox.check(run_open, slack_hook(SEARCH_TOOL, tool_input, None), now=T0) == 0


@pytest.mark.parametrize("change", BAD_MESSAGE_SEARCHES)
def test_check_stops_a_message_search_the_run_doesnt_allow(run_open, slack_hook, capsys, change):
    assert save(run_open, payload(), now=T0) == 0
    search = slack_hook(SEARCH_TOOL, MESSAGE_SEARCH | change, None)
    assert slack_inbox.check(run_open, search, now=T0) == 2
    assert "stopped before it ran (CON-1). Stop the export." in capsys.readouterr().err


def test_check_stops_a_message_search_before_its_channel_is_read(run_open, slack_hook, capsys):
    search = slack_hook(SEARCH_TOOL, MESSAGE_SEARCH, None)
    assert slack_inbox.check(run_open, search, now=T0) == 2
    assert "a channel that hasn't been read yet" in capsys.readouterr().err


def search_files(config) -> list:
    return sorted(slack_inbox.inbox_dir(config.data_dir).rglob("search_messages-*.json"))


def test_a_message_search_is_saved_as_thread_ids_only(run_open, slack_hook, slack_search, capsys):
    assert save(run_open, payload(), now=T0) == 0
    capsys.readouterr()
    found = slack_hook(SEARCH_TOOL, MESSAGE_SEARCH, slack_search(cursor="FAKECURSOR02"), "toolu_s1")
    assert save(run_open, found, now=T0) == 0
    [path] = search_files(run_open)
    assert path.parent.name == open_run(run_open.data_dir).run_id
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "saved_at": "2026-10-01T14:00:00Z",
        "run_id": "20261001T140000Z",
        "tool_name": f"mcp__claude_ai_Slack__{SEARCH_TOOL}",
        "channel_id": CHANNEL,
        "after": "1790860440",
        "cursor": None,
        "next_cursor": "FAKECURSOR02",
        "threads": [THREAD_TS],
    }
    note = hook_note(capsys)
    assert note == (
        "diffdata saved this search: 2 message(s), with new replies in 1 thread(s), to read "
        f"with slack_read_thread: {THREAD_TS}. Next page: call again with cursor `FAKECURSOR02`."
    )
    for private in ("Sam", "late reply", "agenda", "example.com"):
        assert private not in path.read_text(encoding="utf-8") and private not in note


def test_an_empty_message_search_is_saved(run_open, slack_hook, slack_search, capsys):
    assert save(run_open, payload(), now=T0) == 0
    capsys.readouterr()
    empty = slack_search("# Search Results for: \n\nNo results found.\n")
    assert save(run_open, slack_hook(SEARCH_TOOL, MESSAGE_SEARCH, empty), now=T0) == 0
    assert "0 message(s), with no thread replies on this page. No more pages." in hook_note(capsys)
    [path] = search_files(run_open)
    assert json.loads(path.read_text(encoding="utf-8"))["threads"] == []


def test_a_message_search_with_a_result_from_another_channel_is_refused(
    run_open, slack_hook, slack_search, capsys
):
    assert save(run_open, payload(), now=T0) == 0
    results = json.loads((FIXTURES / "search_messages.json").read_text(encoding="utf-8"))
    other = results["results"].replace("(ID: C00000001)", "(ID: C00000002)", 1)
    assert save(run_open, slack_hook(SEARCH_TOOL, MESSAGE_SEARCH, slack_search(other)), T0) == 2
    assert "a channel the export run doesn't name" in capsys.readouterr().err
    assert search_files(run_open) == []


ONE_RESULT = (
    "## Messages (1 result)\n### Result 1 of 1\nChannel: #example-channel (ID: C00000001)\n"
    "From: Sam Sample <sam@example.com> (ID: U00000002) \nTime: 2026-10-05 11:00:00 CDT\n"
    "Message_ts: 1791216000.000140\n"
    "Permalink: [link](https://example.slack.com/archives/C00000001/p1791216000000140)\n"
    "Text: \n"
)


@pytest.mark.parametrize(
    "results",
    [
        "",  # neither results nor "No results found."
        ONE_RESULT.replace("(1 result)", "(2 results)"),  # the count doesn't match
        # A message's text that looks like another result, from another channel.
        ONE_RESULT + "### Result 2 of 1\nChannel: #other (ID: C00000002)\n",
        ONE_RESULT.replace("Message_ts:", "Message_ts: 1791216000.000150\nMessage_ts:"),
        ONE_RESULT.replace("Message_ts: 1791216000.000140", "Message_ts: soon"),
        ONE_RESULT.replace("Permalink:", "Link:"),
        ONE_RESULT.replace("Text: \n", ""),
    ],
)
def test_a_message_search_diffdata_cant_read_is_refused(
    run_open, slack_hook, slack_search, capsys, results
):
    assert save(run_open, payload(), now=T0) == 0
    assert save(run_open, slack_hook(SEARCH_TOOL, MESSAGE_SEARCH, slack_search(results)), T0) == 2
    assert "connector's format may have changed. Stop the export." in capsys.readouterr().err
    assert search_files(run_open) == []


def test_two_channel_reads_saved_together_both_keep_their_check_mark(tmp_path):
    # #21: the check mark used to live in run.json, so two saves could overwrite each other's.
    # Now it's the saved read itself, and saving writes nothing else.
    config = Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel", "other-channel"),
        slack_workspace_url="https://example.slack.com",
    )
    begin(config, ["example-channel=C00000001", "other-channel=C00000002"], now=T0)
    run_file = slack_inbox.run_path(config.data_dir).read_text(encoding="utf-8")
    other = [
        {"type": "text", "text": json.dumps({"messages": "Channel: #other-channel (C00000002)\n"})}
    ]
    assert save(config, payload(call="toolu_a"), now=T0) == 0
    assert save(config, payload(channel="C00000002", call="toolu_b", response=other), now=T0) == 0
    assert slack_inbox.run_path(config.data_dir).read_text(encoding="utf-8") == run_file
    for channel in (CHANNEL, "C00000002"):
        thread = payload(tool="slack_read_thread", channel=channel, call=f"toolu_t{channel}")
        assert save(config, thread, now=T0) == 0
    assert len(inbox_files(config)) == 4


def test_a_run_file_with_the_old_check_mark_still_loads(run_open):
    run = json.loads(slack_inbox.run_path(run_open.data_dir).read_text(encoding="utf-8"))
    slack_inbox.write_json(slack_inbox.run_path(run_open.data_dir), run | {"verified": [CHANNEL]})
    loaded = open_run(run_open.data_dir)
    assert loaded.canvases == [CANVAS] and not loaded.full
    assert not slack_inbox.channel_checked(run_open.data_dir, loaded, CHANNEL)  # not read yet


def test_an_empty_incremental_channel_read_is_normal(config, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    hook = json.loads(payload())
    hook["tool_input"]["oldest"] = "1790860440"
    assert save(config, json.dumps(hook).encode("utf-8"), now=T0) == 0
    note = hook_note(capsys)
    assert "0 message(s). That's normal when nothing was posted since the start point." in note
    assert "unexpected" not in note


@pytest.mark.parametrize("damage", ["not JSON", '{"run_id": "20261001T140000Z"}', "[]"])
def test_check_stops_the_read_when_the_run_file_is_damaged(
    tmp_path, monkeypatch, capsys, slack_hook, damage
):
    # Claude Code lets a call run after any hook failure but exit code 2, so a crash must not
    # let the read through.
    data = tmp_path / "data"
    (tmp_path / "config.toml").write_text(
        '[sources]\nslack_channels = ["example-channel"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f'[local]\ndata_dir = "{data.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    run_file = data / "slack" / "run.json"
    run_file.parent.mkdir(parents=True)
    run_file.write_text(damage, encoding="utf-8")
    read = slack_hook("slack_read_channel", {"channel_id": CHANNEL}, None)
    assert slack_inbox.check_from_hook(read) == 2
    err = capsys.readouterr().err
    assert "so it was stopped" in err and "Stop the export" in err
