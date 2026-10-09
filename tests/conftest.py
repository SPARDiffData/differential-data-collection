"""Shared test helpers. Synthetic data only."""

import json
from pathlib import Path

import pytest

SLACK_FIXTURES = Path(__file__).parent / "fixtures" / "slack"


@pytest.fixture
def slack_response():
    """A Slack connector result in the connector's own shape, made from a fixture file:
    a list of text blocks holding JSON, whose `messages` field is the fixture's text."""

    def make(fixture: str) -> list[dict]:
        text = (SLACK_FIXTURES / fixture).read_text(encoding="utf-8")
        inner = {"messages": text, "pagination_info": "There are no more messages."}
        return [{"type": "text", "text": json.dumps(inner)}]

    return make


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
