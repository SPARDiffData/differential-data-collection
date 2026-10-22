"""Shared test helpers. Synthetic data only."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from diffdata.discover.slack_inbox import begin, save

SLACK_FIXTURES = Path(__file__).parent / "fixtures" / "slack"
LAST_PAGE = "There are no more messages."


@pytest.fixture(autouse=True)
def no_real_config(tmp_path_factory, monkeypatch):
    """No test ever reads the person's real ~/.diffdata/config.toml: point that lookup at
    an empty folder. Tests that need one write it there, or pass a path."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(
        "diffdata.common.config.user_config_file", lambda: home / ".diffdata" / "config.toml"
    )
    return home / ".diffdata" / "config.toml"


@pytest.fixture
def slack_response():
    """A Slack connector result in the connector's own shape, made from a fixture file:
    a list of text blocks holding JSON, whose `messages` field is the fixture's text."""

    def make(fixture: str, cursor: str | None = None) -> list[dict]:
        text = (SLACK_FIXTURES / fixture).read_text(encoding="utf-8")
        pagination = f"To view the next page, use cursor: `{cursor}`" if cursor else LAST_PAGE
        inner = {"messages": text, "pagination_info": pagination}
        return [{"type": "text", "text": json.dumps(inner)}]

    return make


@pytest.fixture
def slack_search():
    """A message search result in the connector's shape, from search_messages.json, or with
    `results` in its place. With a cursor, it says there's a next page."""

    def make(results: str | None = None, cursor: str | None = None) -> list[dict]:
        inner = json.loads((SLACK_FIXTURES / "search_messages.json").read_text(encoding="utf-8"))
        if results is not None:
            inner["results"] = results
        if cursor:
            inner["pagination_info"] = f"For the next page of results use cursor `{cursor}`\n"
        return [{"type": "text", "text": json.dumps(inner)}]

    return make


@pytest.fixture
def slack_run(slack_hook):
    """One /slack-export run as the hook saves it, for #example-channel: begin, then each
    read as (tool, tool_input, response), saved a minute apart. Returns begin's report."""

    def run(config, when, reads, full=False) -> str:
        report = begin(config, ["example-channel=C00000001"], now=when, full=full)
        for i, (tool, tool_input, response) in enumerate(reads, start=1):
            payload = slack_hook(tool, tool_input, response, f"toolu_{when:%d%H%M}{i:02}")
            assert save(config, payload, now=when + timedelta(minutes=i)) == 0
        return report

    return run


@pytest.fixture
def slack_hook():
    """What Claude Code sends the /slack-export hook on stdin after a Slack tool call."""

    def make(tool: str, tool_input: dict, tool_response, call: str = "toolu_fake0001") -> bytes:
        payload = {
            "session_id": "fake-session",
            "transcript_path": "/fake/transcript.jsonl",
            "hook_event_name": "PostToolUse",
            "tool_name": f"mcp__claude_ai_Slack__{tool}",
            "tool_input": tool_input,
            "tool_response": tool_response,
            "tool_use_id": call,
        }
        return json.dumps(payload).encode("utf-8")

    return make
