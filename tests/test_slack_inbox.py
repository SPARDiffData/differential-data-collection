"""The Slack inbox: export runs, and the hook that saves raw results (DIS-1). Synthetic data."""

import json
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


def payload(tool="slack_read_channel", channel=CHANNEL, call="toolu_0001"):
    return json.dumps(
        {
            "session_id": "fake-session",
            "transcript_path": "/fake/transcript.jsonl",
            "hook_event_name": "PostToolUse",
            "tool_name": f"mcp__claude_ai_Slack__{tool}",
            "tool_input": {"channel_id": channel, "response_format": "detailed"},
            "tool_response": [{"type": "text", "text": "synthetic result"}],
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
    assert saved["tool_response"] == [{"type": "text", "text": "synthetic result"}]
    assert saved["tool_input"]["channel_id"] == CHANNEL
    assert saved["saved_at"] == "2026-10-01T14:01:00Z"
    assert "session_id" not in saved and "transcript_path" not in saved


@pytest.mark.parametrize(
    "other",
    [
        payload(channel="C00000002"),  # a channel this run didn't name
        payload(tool="slack_search_public_and_private"),  # not a tool we save
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
