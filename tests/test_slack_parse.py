"""Parsing the Slack connector's results (DIS-1). Synthetic fixtures only."""

import json
from dataclasses import asdict

import pytest

from diffdata.discover.slack_parse import (
    next_cursor,
    parse_files,
    parse_result,
    permalink,
    result_text,
    search_messages,
    to_markdown,
)

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


def test_unwraps_a_saved_copy_that_holds_the_block_list(slack_response):
    saved_copy = json.dumps(slack_response("read_channel.txt"))  # one string, as read from a file
    assert result_text(saved_copy).startswith("Channel: #example-channel")


def test_finds_the_next_cursor():
    more = "There are more messages available. To view the next page, use cursor: `FAKECURSOR01`\n"
    result = json.dumps({"messages": "", "pagination_info": more})
    assert next_cursor(result) == "FAKECURSOR01"
    assert next_cursor(json.dumps({"messages": "", "pagination_info": "No more."})) is None


def test_counts_replies_from_the_thread_line(channel):
    assert {ts: m.replies for ts, m in channel.items() if m.replies} == {PARENT: 3}


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


def test_a_file_name_keeps_its_commas():
    files = parse_files(
        "notes, final.txt (ID: F00000004, text/plain, 1.0 KB), "
        "plan (v2), draft.png (ID: F00000005, image/png, 2.0 MB)"
    )
    assert files == [
        {"id": "F00000004", "name": "notes, final.txt", "type": "text/plain"},
        {"id": "F00000005", "name": "plan (v2), draft.png", "type": "image/png"},
    ]


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


def thread_parent(
    author: str = "Alex Example <alex@example.com> (U00000001)", ts: str = PARENT, text="Hello"
) -> str:
    return (
        "=== THREAD PARENT MESSAGE ===\n"
        f"From: {author}\n"
        "Time: 2026-10-01 09:14:00 CDT\n"
        f"Message TS: {ts}\n"
        f"{text}\n"
    )


@pytest.mark.parametrize(
    ("author", "name"),
    [
        ("Alex Example <alex@example.com>(U00000001)", "Alex Example"),  # no space before the ID
        ("Alex Example <alex@example.com>  (U00000001)", "Alex Example"),
        ("Alex Example (they/them) <alex@example.com> (U00000001)", "Alex Example (they/them)"),
    ],
)
def test_the_author_name_never_keeps_the_email(author, name):
    [message], skipped = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(author))
    assert (message.user_name, message.user_id, skipped) == (name, "U00000001", 0)
    assert "example.com" not in json.dumps(asdict(message))


@pytest.mark.parametrize(
    "author",
    [
        "Alex Example alex@example.com (U00000001)",  # an email outside the brackets
        "Alex Example <alex@example.com>",  # no user ID
    ],
)
def test_an_author_line_it_cant_read_is_counted_and_not_kept(author):
    assert parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(author)) == ([], 1)


@pytest.mark.parametrize(
    "line",
    [
        "Reactions: mostly positive, see the doc",
        "Thread: let's keep replies short",
        "Files: everything is in the shared folder",
        "Thread: 2 replies so far",
        "Reactions: +1",
    ],
)
def test_text_that_only_looks_like_a_detail_line_is_kept(line):
    text = f"Quick update.\n{line}"
    [message], skipped = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert (message.text, message.reactions, message.files, skipped) == (text, [], [], 0)


def test_detail_lines_after_a_look_alike_are_still_details():
    text = (
        "Thread: notes below\n"
        "Thread: 1 reply (latest: 2026-10-01 09:20:00 CDT)\n"
        "Reactions: eyes (1)"
    )
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert message.text == "Thread: notes below"
    assert (message.replies, message.reactions) == (1, [{"name": "eyes", "count": 1}])


def test_a_files_line_with_a_comma_in_the_name_is_still_a_detail():
    text = "See attached.\nFiles: notes, final.txt (ID: F00000004, text/plain, 1.0 KB)"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert message.text == "See attached."
    assert [f["id"] for f in message.files] == ["F00000004"]


@pytest.mark.parametrize(
    "ts",
    [
        "not-a-ts",
        "1790864040",
        "1790864040.",
        ".000100",
        "1790864040.0001.00",
        "99999999999.000100",
    ],
)
def test_a_malformed_message_ts_is_counted_and_not_kept(ts):
    assert parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(ts=ts)) == ([], 1)


def test_a_message_search_gives_each_messages_channel_ts_and_thread_only(slack_search):
    # A reply names its thread in its permalink; a top-level post doesn't.
    assert search_messages(slack_search()) == [
        (CHANNEL, "1791216000.000140", PARENT),
        (CHANNEL, "1791212400.000600", None),
    ]


def test_an_empty_message_search_gives_no_messages(slack_search):
    assert search_messages(slack_search("# Search Results for: \n\nNo results found.\n")) == []


@pytest.mark.parametrize("response", [None, [], [{"type": "text", "text": '{"messages": "x"}'}]])
def test_a_message_search_without_results_gives_none(response):
    assert search_messages(response) is None


def test_a_message_search_without_page_information_gives_none(slack_search):
    inner = json.loads(slack_search()[0]["text"])
    del inner["pagination_info"]
    assert search_messages([{"type": "text", "text": json.dumps(inner)}]) is None
