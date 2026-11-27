"""Shared test helpers. Synthetic data only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata.discover.slack_inbox import begin, save

SLACK_FIXTURES = Path(__file__).parent / "fixtures" / "slack"
LAST_PAGE = "There are no more messages available.\n"


@pytest.fixture(autouse=True)
def not_under_export(monkeypatch):
    """Tests run the same inside a Claude Code that `diffdata export` started (#159)."""
    monkeypatch.delenv("DIFFDATA_EXPORT", raising=False)


@pytest.fixture(autouse=True)
def no_real_config(tmp_path_factory, monkeypatch):
    """No test ever reads the person's real ~/.diffdata/config.toml: point that lookup at
    an empty folder. Tests that need one write it there, or pass a path."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(
        "diffdata.common.config.user_config_file", lambda: home / ".diffdata" / "config.toml"
    )
    return home / ".diffdata" / "config.toml"


class FakeTeamLists:
    """The team folders' opt-out lists, as the store would serve them to an export's start
    (discover/team_optouts.py), so no test reaches Google. By default every team has an empty
    list written long after any test's notice. Tests change `lists`, `written` or `error`."""

    WRITTEN = datetime(2030, 1, 1, tzinfo=UTC)

    def __init__(self):
        self.lists: dict[str, bytes] | None = None  # object name -> bytes; None: the default
        self.written = self.WRITTEN
        self.error: Exception | None = None
        self.asked: list[tuple[str, str]] = []  # (bucket, object name)

    @staticmethod
    def body(team: str, ids: list[str]) -> bytes:
        data = {"format": "diffdata-opted-out", "version": 1, "team": team, "slack_opted_out": ids}
        return json.dumps(data).encode("utf-8")

    def __call__(self, bucket: str, key: dict, name: str):
        self.asked.append((bucket, name))
        if self.error is not None:
            raise self.error
        if self.lists is None:
            return self.body(name.split("/")[0], []), self.written
        return (self.lists[name], self.written) if name in self.lists else None


def team_store(tmp_path: Path) -> dict:
    """Config settings for a made-up team folder, with a made-up key next to config.toml, so
    an export's start fetches the team's opt-out list (from FakeTeamLists)."""
    home = tmp_path / "fake-diffdata"
    home.mkdir(parents=True, exist_ok=True)
    key = {
        "type": "service_account",
        "client_email": "fake-uploader@fake-project.iam.gserviceaccount.com",
        "private_key": "FAKE-NOT-A-KEY",
    }
    (home / "store-key.json").write_text(json.dumps(key), encoding="utf-8")
    return {
        "store_bucket": "fake-bucket",
        "store_folder": "fake-team",
        "path": home / "config.toml",
    }


@pytest.fixture(autouse=True)
def team_lists(monkeypatch):
    fake = FakeTeamLists()
    monkeypatch.setattr("diffdata.discover.team_optouts.gcs_fetch", fake)
    return fake


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
