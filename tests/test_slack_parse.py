"""Parsing the Slack connector's results (DIS-1). Synthetic fixtures only."""

import json
from dataclasses import asdict

import pytest

from diffdata.discover.slack_parse import (
    next_cursor,
    pages_known,
    parse_files,
    parse_result,
    permalink,
    result_text,
    search_messages,
    to_markdown,
    unread_thread_lines,
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


@pytest.mark.parametrize(
    "pagination", [None, "", "For the next page of results use the cursor below.", "Something new."]
)
def test_a_message_search_without_page_information_it_knows_gives_none(slack_search, pagination):
    inner = json.loads(slack_search()[0]["text"])
    if pagination is None:
        del inner["pagination_info"]
    else:
        inner["pagination_info"] = pagination
    assert search_messages([{"type": "text", "text": json.dumps(inner)}]) is None


@pytest.mark.parametrize(
    "pagination, known",
    [
        ("There are no more messages available.\n", True),  # a channel's last page
        ("There are no more messages in this thread.\n", True),  # a thread's
        ("End of results - No more pages available.\\n", True),  # a search's, as sent
        ("There are more messages available. To view the next page, use cursor: `FAKE01`", True),
        ("For the next page of results use cursor `FAKE02`\n", True),
        ("There are more messages available.", False),  # a next page with no cursor
        ("", False),
    ],
)
def test_page_information_is_known_only_in_the_connectors_wording(pagination, known):
    response = [{"type": "text", "text": json.dumps({"pagination_info": pagination})}]
    assert pages_known(response) is known


# Forwarded messages (#124): the connector writes one line for each after the post's detail
# lines, in this exact shape.
FWD_TAIL = " — use slack_read_thread or slack_read_channel to read it in context"


def forwarded(
    name="Sam Sample", link="https://example.slack.com/archives/C00000009/p1791201600000600"
):
    return f"Forwarded message from {name} ({link}): A forwarded note.{FWD_TAIL}"


@pytest.fixture
def forwarding(slack_response):
    messages, skipped = parse_result(
        CHANNEL_TOOL, {"channel_id": CHANNEL}, slack_response("read_channel_forwarded.txt")
    )
    assert skipped == 0
    return {m.ts: m for m in messages}


def test_a_post_with_one_forwarded_message_keeps_its_details(forwarding):
    post = forwarding["1791208800.000600"]
    assert post.replies == 1
    assert post.reactions == [{"name": "eyes", "count": 1}]
    assert [f["id"] for f in post.files] == ["F00000020"]
    assert post.text.split("\n")[0] == "Sharing this here."
    assert post.text.split("\n")[1].startswith("Forwarded message from Alex Example (")
    assert "Thread:" not in post.text and "Reactions:" not in post.text


def test_a_post_with_two_forwarded_messages_keeps_its_details(forwarding):
    post = forwarding["1791298800.000700"]
    assert post.replies == 2
    lines = post.text.split("\n")
    assert lines[0] == "Two notes from the other channel, for Monday."
    assert [line.split(" (")[0] for line in lines[1:]] == [
        "Forwarded message from Sam Sample",
        "Forwarded message from Alex Example",
    ]
    assert "First forwarded note." in lines[1] and "Second forwarded note." in lines[2]


def test_forwarded_lines_dont_change_a_post_without_details():
    text = f"Look at this.\n{forwarded()}"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert (message.text, message.replies, message.reactions) == (text, 0, [])


@pytest.mark.parametrize(
    "look_alike",
    [
        "Forwarded message from Sam Sample: see below",  # no link
        "Forwarded message from Sam Sample (https://example.slack.com/archives/C00000009/p1)",
        forwarded().removesuffix(" in context"),  # not the connector's whole wording
        forwarded(link="https://example.com/not-slack"),  # not a message link
        forwarded() + " and more",  # more after the connector's wording
    ],
)
def test_a_line_that_only_starts_like_a_forwarded_message_cant_hide_a_detail_line(look_alike):
    # As the last line, after a detail line, it's text: the Thread line before it is text too,
    # as it was before #124, rather than a guess.
    text = f"Hello\nThread: 1 reply (latest: 2026-10-01 09:20:00 CDT)\n{look_alike}"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert (message.text, message.replies) == (text, 0)


def test_a_forwarded_look_alike_in_the_text_stays_where_it_is():
    # An exact copy typed into the message's own text, above its details, isn't moved.
    text = f"Hello\n{forwarded()}\nmore words\nReactions: eyes (1)"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert message.text == f"Hello\n{forwarded()}\nmore words"
    assert message.reactions == [{"name": "eyes", "count": 1}]


def test_a_detail_line_typed_above_the_real_one_cant_overwrite_it():
    # Codex on #125: the connector's own Thread line is the last one, so it wins.
    real = "Thread: 2 replies (latest: 2026-10-01 09:20:00 CDT)"
    text = f"Hello\nThread: 0 replies (latest: 2026-10-01 09:14:00 CDT)\n{real}\n{forwarded()}"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert message.replies == 2


@pytest.mark.parametrize(
    ("body", "kept"), [("Hello\n  \n", "Hello"), ("  \nHello\n  ", "  \nHello")]
)
def test_space_only_lines_at_the_end_are_dropped_as_before(body, kept):
    # Codex on #125: a post with no details keeps the same text it had before #124. Only
    # blank lines at the end go; one at the start stays.
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=body))
    assert message.text == kept


def test_a_blank_line_between_forwarded_messages_is_kept():
    text = f"Notes\nReactions: eyes (1)\n  \n{forwarded()}\n\n{forwarded(name='Alex Example')}\n\t"
    [message], _ = parse_result(THREAD_TOOL, {"message_ts": PARENT}, thread_parent(text=text))
    assert message.text == f"Notes\n{forwarded()}\n\n{forwarded(name='Alex Example')}"


# #159: the connector writes the detail lines with "> " before them, at least the Thread line.
QUOTED_DETAILS = (
    "Hello there.\n"
    "\n"
    "> Thread: 12 replies (latest: 2026-10-01 09:20:00 CDT)\n"
    "> Reactions: +1 (2), eyes (1)\n"
    "> Files: notes.txt (ID: F00000009, text, 1 KB)"
)


def channel_page(*bodies: str) -> str:
    """A made-up channel read, one post per body, in the connector's wrapping."""
    posts = "".join(
        f"=== Message from Sam Sample <sam@example.com> (U00000002) at 2026-10-0{i + 1} "
        f"09:00:00 CDT ===\nMessage TS: 179086{i}000.000100\n{body}\n\n"
        for i, body in enumerate(bodies)
    )
    text = "Channel: #example-channel (C00000001)\n\n" + posts
    return json.dumps(
        {"messages": text, "pagination_info": "There are no more messages available."}
    )


def test_detail_lines_with_a_quote_mark_are_read():
    [post], skipped = parse_result(CHANNEL_TOOL, {}, channel_page(QUOTED_DETAILS))
    assert (post.text, post.replies, skipped) == ("Hello there.", 12, 0)
    assert post.reactions == [{"name": "+1", "count": 2}, {"name": "eyes", "count": 1}]
    assert post.files == [{"id": "F00000009", "name": "notes.txt", "type": "text"}]


@pytest.mark.parametrize("count", ["1 reply", "9 replies", "42 replies"])
def test_a_quoted_thread_line_counts_replies_with_or_without_trailing_space(count):
    body = f"Hi\n> Thread: {count} (latest: 2026-10-01 09:20:00 CDT) "
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert (post.text, post.replies) == ("Hi", int(count.split()[0]))
    assert unread_thread_lines(channel_page(body)) == 0


@pytest.mark.parametrize(
    "line",
    [
        "> Thread: see below",
        "> Reactions: mostly positive",
        "> Files: in the shared folder",
        ">> Reactions: +1 (2)",
        ">Reactions: +1 (2)",
        "> Thread: 2 replies so far",
    ],
)
def test_must_refuse_2_text_that_only_looks_like_a_quoted_detail_line_stays_text(line):
    text = f"Quick update.\n{line}"
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(text))
    assert (post.text, post.replies, post.reactions, post.files) == (text, 0, [], [])
    # Higher up in a post's text, a look-alike never stops the export. Last, where the Thread
    # line goes, "> Thread: 2 replies so far" would (see the tests below).
    assert unread_thread_lines(channel_page(f"{text}\nThanks.")) == 0


def test_the_unquoted_detail_lines_still_count_as_before():
    body = "Hi\nThread: 3 replies (latest: 2026-10-01 09:20:00 CDT)\nReactions: +1 (1)"
    assert unread_thread_lines(channel_page(body)) == 0
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert post.replies == 3


@pytest.mark.parametrize(
    "line",
    [
        ">> Thread: 3 replies (latest: 2026-10-01 09:20:00 CDT)",  # two quote marks
        "> *Thread:* 3 replies (latest: 2026-10-01 09:20:00 CDT)",  # bold
        "> Thread: 3 replies",  # no latest part
        "> Thread: 3 replies (newest: 2026-10-01 09:20:00 CDT)",
        "  thread - 3 replies",
    ],
)
def test_must_refuse_1_a_thread_line_the_parser_didnt_read_is_counted(line):
    page = channel_page(f"Hi\n{line}", "A second post.")
    [first, _], _ = parse_result(CHANNEL_TOOL, {}, page)
    assert first.replies == 0
    assert unread_thread_lines(page) == 1


def test_a_thread_line_before_the_first_post_is_counted_but_not_in_an_unreadable_one():
    page = json.loads(channel_page("Hi"))
    page["messages"] = page["messages"].replace(
        "Channel: #example-channel (C00000001)\n",
        "Channel: #example-channel (C00000001)\nThread: 2 replies\n",
    )
    assert unread_thread_lines(json.dumps(page)) == 1
    unreadable = (
        "=== Message from Sam Sample <sam@example.com> (U00000002) at 2026-10-01 09:00:00 CDT "
        "===\nNo TS line here\nThread: 2 replies (latest: 2026-10-01 09:20:00 CDT)\n"
    )
    # parse_result counts that post as unreadable, which holds the start point back already.
    assert parse_result(CHANNEL_TOOL, {}, json.dumps({"messages": unreadable})) == ([], 1)
    assert unread_thread_lines(json.dumps({"messages": unreadable})) == 0


# Fix round on #160: the guard only looks where the Thread line goes.
@pytest.mark.parametrize("line", ["Thread (2 replies)", "Thread: 5 replies", "thread - 2 replies!"])
def test_a_thread_look_alike_in_the_middle_of_a_post_isnt_counted(line):
    page = channel_page(f"Recap\n{line}\nthanks")
    assert unread_thread_lines(page) == 0


@pytest.mark.parametrize(
    "line",
    [
        "> Thread: 3 replies, latest: 2026-10-01 09:20:00 CDT",
        "> Thread: 3 replies \u00b7 latest 2026-10-01",
        "Thread (2 replies)",
    ],
)
def test_at_the_thread_lines_place_any_wording_after_the_count_is_counted(line):
    page = channel_page(f"Hi\n{line}\nReactions: +1 (1)")
    assert unread_thread_lines(page) == 1


def multi_line_forward(text: str) -> str:
    return (
        "Forwarded message from Alex Example "
        "(https://example.slack.com/archives/C00000001/p1790860000000100): "
        f"{text} \u2014 use slack_read_thread or slack_read_channel to read it in context"
    )


def test_a_forwarded_message_over_several_lines_doesnt_hide_the_thread_line():
    forward = multi_line_forward("line one\nline two\nline three")
    body = f"Hi\n> Thread: 3 replies (latest: 2026-10-01 09:20:00 CDT)\n{forward}"
    [post, _], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body, "second"))
    assert post.replies == 3
    assert post.text == f"Hi\n{forward}"
    assert unread_thread_lines(channel_page(body)) == 0


def test_a_line_that_only_ends_like_a_forward_doesnt_count_as_one():
    # No start line above it: it's text, and the scan stops there as before.
    body = (
        "Hi\nnot a forward \u2014 use slack_read_thread or slack_read_channel to read it in context"
    )
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert post.text == body


@pytest.mark.parametrize("quote", ["&gt; ", "&gt;"])
def test_detail_lines_quoted_as_slack_escapes_them_are_read(quote):
    body = f"Hi\n{quote}Thread: 3 replies (latest: x)\n{quote}Reactions: eyes (1)"
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert (post.text, post.replies, post.reactions) == ("Hi", 3, [{"name": "eyes", "count": 1}])
    assert unread_thread_lines(channel_page(body)) == 0


def test_an_escaped_quote_on_a_thread_line_in_another_shape_is_counted():
    assert unread_thread_lines(channel_page("Hi\n&gt;&gt; Thread: 3 replies (latest: x)")) == 1


@pytest.mark.parametrize(
    ("count", "n"), [("1,024", 1024), ("12,345,678", 12345678), ("1024", 1024)]
)
def test_a_reply_count_with_thousands_commas_is_read(count, n):
    body = f"Hi\n> Thread: {count} replies (latest: x)"
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert post.replies == n
    assert unread_thread_lines(channel_page(body)) == 0


def test_a_badly_placed_comma_isnt_a_count():
    body = "Hi\n> Thread: 1,02 replies (latest: x)"
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert post.replies == 0
    assert unread_thread_lines(channel_page(body)) == 1  # at the Thread line's place


# Fix round 2 on #160: the guard passes Reactions and Files lines in any wording, and
# forwards that only end like one, so a format change there can't hide a Thread line.
THREAD_LINE = "> Thread: 3 replies (latest: 2026-10-01 09:20:00 CDT)"
BAD_OPENING = (
    "Forwarded message from Alex (https://example.slack.com/archives/C1/p1?x=(y)): line one"
)
FORWARD_ENDING = "line two \u2014 use slack_read_thread or slack_read_channel to read it in context"


@pytest.mark.parametrize(
    "under",
    [
        "> Reactions: :+1: 2",
        "&gt; Reactions: eyes x1",
        "> Files: notes.txt (F00000009)",
        "Files: notes.txt",
        f"{BAD_OPENING}\n{FORWARD_ENDING}",  # its opening isn't in FORWARDED_START's shape
        FORWARD_ENDING,  # an ending with no opening at all
        "Forwarded message from Alex (https://x/y): one\n" + FORWARD_ENDING,
    ],
)
def test_must_refuse_1_a_thread_line_above_lines_in_a_new_shape_is_counted(under):
    page = channel_page(f"Hi\n{THREAD_LINE}\n{under}")
    [post], _ = parse_result(CHANNEL_TOOL, {}, page)
    assert post.replies == 0  # the parser stopped at the line it doesn't know
    assert THREAD_LINE in post.text  # and kept everything as text, as before
    assert unread_thread_lines(page) == 1


def test_the_guards_wider_scan_doesnt_change_what_the_parser_keeps():
    body = "Hi\nReactions: mostly positive"
    [post], _ = parse_result(CHANNEL_TOOL, {}, channel_page(body))
    assert (post.text, post.reactions) == (body, [])
    assert unread_thread_lines(channel_page(body)) == 0
