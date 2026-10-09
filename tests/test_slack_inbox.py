"""The Slack inbox: export runs, and the hook that saves raw results (DIS-1). Synthetic data."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from diffdata import cli
from diffdata.common.config import Config
from diffdata.discover import slack_inbox
from diffdata.discover.slack_inbox import SlackExportError, abort, begin, open_run, save

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
CHANNEL = "C00000001"


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
