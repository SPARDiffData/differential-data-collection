"""Parsing the Slack connector's results (DIS-1). Synthetic fixtures only."""

import json
from dataclasses import asdict

import pytest

from diffdata.discover.slack_parse import parse_result, permalink, result_text, to_markdown

CHANNEL_TOOL = "mcp__claude_ai_Slack__slack_read_channel"
THREAD_TOOL = "mcp__claude_ai_Slack__slack_read_thread"
CHANNEL = "C00000001"
PARENT = "1790864040.000100"


@pytest.fixture
def channel(slack_response):
    messages, skipped = parse_result(
        CHANNEL_TOOL, {"channel_id": CHANNEL}, slack_response("read_channel.txt")
    )
    assert skipped == 0
    return {m.ts: m for m in messages}


@pytest.fixture
def thread(slack_response):
    messages, skipped = parse_result(
        THREAD_TOOL,
        {"channel_id": CHANNEL, "message_ts": PARENT},
        slack_response("read_thread.txt"),
    )
    assert skipped == 0
    return messages


def test_unwraps_the_connector_result(slack_response):
    assert result_text(slack_response("read_channel.txt")).startswith("Channel: #example-channel")


def test_reads_every_top_level_post(channel):
    assert len(channel) == 5
    kickoff = channel[PARENT]
    assert (kickoff.user_id, kickoff.user_name, kickoff.bot) == ("U00000001", "Alex Example", False)
    assert kickoff.thread_ts is None
    # The Thread and Reactions lines are details, not part of the text.
    assert kickoff.text == (
        "Kickoff notes are in the doc: "
        "<https://docs.google.com/document/d/FAKE-DOC-0001/edit|Kickoff notes>"
    )
    assert kickoff.reactions == [{"name": "+1", "count": 2}]


def test_keeps_multiline_text_and_several_reactions(channel):
    assert channel["1790870700.000200"].text.endswith("\nA second line in the same message.")
    assert channel["1790964000.000400"].reactions == [
        {"name": "white_check_mark", "count": 1},
        {"name": "eyes", "count": 2},
    ]


def test_keeps_slackbot_canvas_notices_as_bot_posts(channel):
    notice = channel["1790955177.000300"]
    assert (notice.user_id, notice.user_name, notice.bot) == ("USLACKBOT", "Slackbot", True)
    assert notice.text == "Alex Example made updates to a canvas tab: F00000002"


def test_thread_replies_point_at_their_parent(thread):
    assert [m.ts for m in thread] == [
        PARENT,
        "1790864400.000110",
        "1790865060.000120",
        "1791018120.000130",
    ]
    assert [m.thread_ts for m in thread] == [None, PARENT, PARENT, PARENT]


def test_lists_attached_files(thread):
    assert thread[2].files == [{"id": "F00000003", "name": "call-notes.txt", "type": "text/plain"}]
    assert thread[2].text == "Thanks! Notes from Tuesday's call are attached."


def test_author_emails_are_not_kept(channel, thread):
    kept = json.dumps([asdict(m) for m in [*channel.values(), *thread]])
    assert "alex@example.com" not in kept
    assert "sam@example.com" not in kept


def test_the_parent_reads_the_same_from_channel_and_thread(channel, thread):
    assert thread[0].content_hash() == channel[PARENT].content_hash()


def test_reactions_dont_make_a_new_version(channel):
    message = channel[PARENT]
    before = message.content_hash()
    message.reactions = [{"name": "tada", "count": 5}]
    assert message.content_hash() == before
    message.text += " (edited)"
    assert message.content_hash() != before


def test_a_block_it_cant_read_is_counted_not_dropped_silently():
    text = "=== THREAD PARENT MESSAGE ===\nSomething new: the format changed\n"
    messages, skipped = parse_result(THREAD_TOOL, {"message_ts": PARENT}, text)
    assert (messages, skipped) == ([], 1)


def test_markdown_mentions_links_and_escapes():
    slack = (
        "Hi <@U00000001|Alex Example> and <@U00000002>, see <#C00000001|example-channel> "
        "and <#C00000009>. <https://example.com/a|The doc>, <https://example.com/b>, "
        "<mailto:sam@example.com|sam@example.com>. <!here> <!subteam^S00000001|@team> "
        "&amp; &lt;b&gt;"
    )
    assert to_markdown(slack, people={"U00000002": "Sam Sample"}, channels={}) == (
        "Hi @Alex Example and @Sam Sample, see #example-channel and #C00000009. "
        "[The doc](https://example.com/a), https://example.com/b, sam@example.com. "
        "@here @team & <b>"
    )


def test_permalinks():
    workspace = "https://example.slack.com"
    assert permalink(workspace, CHANNEL, PARENT) == (
        "https://example.slack.com/archives/C00000001/p1790864040000100"
    )
    assert permalink(workspace, CHANNEL, "1790864400.000110", PARENT) == (
        "https://example.slack.com/archives/C00000001/p1790864400000110"
        "?thread_ts=1790864040.000100&cid=C00000001"
    )
