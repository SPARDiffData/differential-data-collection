"""Start points for incremental Slack exports (DIS-1): a channel's start moves forward only
after a complete run. Synthetic fixtures only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata.common.config import Config
from diffdata.discover import slack_state
from diffdata.discover.slack_export import build
from diffdata.discover.slack_inbox import abort, begin, open_run, run_path, save
from diffdata.discover.slack_state import read_state, state_path

CHANNEL = "C00000001"
PAIR = "example-channel=C00000001"
PARENT = "1790864040.000100"
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=2)
START = "1791111600"  # T0 less the hour of overlap, in Unix seconds
LATER = "1791115200"  # T0 itself, which would skip the overlap
SEARCH_TOOL = "slack_search_public_and_private"
NO_RESULTS = "# Search Results for: \n\nNo results found.\n"
FIXTURES = Path(__file__).parent / "fixtures" / "slack"


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
    )


def channel(response, oldest=None, cursor=None):
    tool_input = {"channel_id": CHANNEL, "response_format": "detailed", "limit": 100}
    tool_input |= {"oldest": oldest} if oldest else {}
    tool_input |= {"cursor": cursor} if cursor else {}
    return "slack_read_channel", tool_input, response


def thread(response, ts=PARENT):
    tool_input = {"channel_id": CHANNEL, "message_ts": ts, "response_format": "detailed"}
    return "slack_read_thread", tool_input | {"limit": 1000}, response


def search(response, after=START):
    tool_input = {
        "content_types": "messages",
        "filters": f"in:<#{CHANNEL}>",
        "channel_types": "public_channel,private_channel",
        "natural_language_query": "",
        "response_format": "detailed",
        "include_bots": True,
        "include_context": False,
        "sort": "timestamp",
        "limit": 20,
    }
    return SEARCH_TOOL, tool_input | ({"after": after} if after else {}), response


@pytest.fixture
def first_export(config, slack_run, slack_response):
    """A complete first export at T0: the whole channel, then its one thread with replies."""
    reads = [channel(slack_response("read_channel.txt")), thread(slack_response("read_thread.txt"))]
    slack_run(config, T0, reads)
    return build(config)


@pytest.fixture
def incremental(slack_response, slack_search):
    """A complete incremental run's reads, from START: the new post, the search that finds a
    late reply in an older thread, and that thread."""
    return [
        channel(slack_response("read_channel_new.txt"), oldest=START),
        search(slack_search()),
        thread(slack_response("read_thread_late.txt")),
    ]


def test_with_no_earlier_run_the_export_starts_from_the_beginning(config):
    assert f"{CHANNEL}: from the start" in begin(config, [PAIR], now=T0)


def test_a_complete_run_sets_the_start_point_and_the_next_starts_an_hour_before_it(
    config, first_export
):
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-04T12:00:00Z"}
    assert "1 of 1 channel(s) moved their start point forward." in first_export
    assert f'{CHANNEL}: from "{START}" (2026-10-04 11:00 UTC)' in begin(config, [PAIR], now=T1)


def test_a_complete_incremental_run_moves_the_start_point(
    config, first_export, slack_run, incremental
):
    slack_run(config, T1, incremental)
    assert "1 of 1 channel(s) moved their start point forward." in build(config)
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-06T12:00:00Z"}


def drop(reads: list, tool: str) -> list:
    return [read for read in reads if read[0] != tool]


@pytest.mark.parametrize(
    "change, why",
    [
        # A test run reads one page of 5 and leaves its cursor.
        ("test run", slack_state.PAGES),
        ("thread from the search not read", slack_state.THREADS),
        ("thread from the channel read not read", slack_state.THREADS),
        ("search skipped", slack_state.NO_SEARCH),
        ("search not finished", slack_state.NO_SEARCH),
        ("search started late", slack_state.NO_SEARCH),
        ("channel read started late", slack_state.LATE),
    ],
)
def test_an_incomplete_run_leaves_the_start_point_where_it_was(
    config, first_export, slack_run, slack_response, slack_search, incremental, change, why
):
    reads = {
        "test run": [channel(slack_response("read_channel_new.txt", "FAKECURSOR01"), START)],
        "thread from the search not read": drop(incremental, "slack_read_thread"),
        "thread from the channel read not read": [
            channel(slack_response("read_channel.txt"), oldest=START),
            search(slack_search(NO_RESULTS)),
        ],
        "search skipped": drop(incremental, SEARCH_TOOL),
        "search not finished": [
            incremental[0],
            search(slack_search(cursor="FAKECURSOR02")),
            incremental[2],
        ],
        "search started late": [incremental[0], search(slack_search(), LATER), incremental[2]],
        "channel read started late": [
            channel(slack_response("read_channel_new.txt"), oldest=LATER),
            *incremental[1:],
        ],
    }[change]
    slack_run(config, T1, reads)
    report = build(config)
    assert "0 of 1 channel(s) moved their start point forward." in report
    assert f"the next run reads the same stretch again: 1 {why}." in report
    assert CHANNEL not in report and "example-channel" not in report  # counts only
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-04T12:00:00Z"}
    # So the next run covers the same stretch again.
    assert f'from "{START}"' in begin(config, [PAIR], now=T1 + timedelta(hours=1))


THREAD = (FIXTURES / "read_thread_late.txt").read_text(encoding="utf-8")


def text_block(value) -> list[dict]:
    return [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}]


@pytest.mark.parametrize(
    "response",
    [
        text_block("Error: thread_not_found"),  # not the connector's JSON
        text_block({"error": "thread_not_found"}),
        text_block({"messages": "", "pagination_info": "There are no more messages."}),
        text_block({"messages": THREAD}),  # no page information, so no way to tell the last page
        # Another thread's read, saved under this thread's ts.
        text_block(
            {
                "messages": THREAD.replace("1790864040.000100", "1790870700.000200"),
                "pagination_info": "There are no more messages.",
            }
        ),
    ],
)
def test_a_thread_read_that_failed_doesnt_count_even_after_an_abort(
    config, first_export, slack_run, incremental, response
):
    # The hook saves whatever a thread read returns, and says when it held no messages. The
    # run is then aborted, and a later build still mustn't count that thread as read.
    slack_run(config, T1, [*incremental[:2], thread(response)])
    abort(config)
    build(config)
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-04T12:00:00Z"}


def test_a_channel_page_without_page_information_doesnt_count(config, slack_run):
    page = text_block({"messages": "Channel: #example-channel (C00000001)\n"})
    slack_run(config, T0, [channel(page)])
    assert f"1 {slack_state.PAGES}" in build(config)
    assert read_state(config.data_dir) == {}


def test_a_first_export_must_read_from_the_beginning(config, slack_run, slack_response):
    reads = [channel(slack_response("read_channel.txt"), oldest=START)]
    slack_run(config, T0, reads + [thread(slack_response("read_thread.txt"))])
    assert f"1 {slack_state.LATE}" in build(config)
    assert read_state(config.data_dir) == {}


def test_a_first_export_that_skipped_a_thread_isnt_complete(config, slack_run, slack_response):
    slack_run(config, T0, [channel(slack_response("read_channel.txt"))])
    assert f"1 {slack_state.THREADS}" in build(config)
    assert read_state(config.data_dir) == {}


def test_a_later_page_completes_the_channel_read(config, slack_run, slack_response):
    reads = [
        channel(slack_response("read_channel.txt", "FAKECURSOR01")),
        channel(slack_response("read_channel_new.txt"), cursor="FAKECURSOR01"),
        thread(slack_response("read_thread.txt")),
    ]
    slack_run(config, T0, reads)
    assert "1 of 1 channel(s) moved" in build(config)


def test_a_run_whose_canvases_werent_read_still_moves_the_start_point(
    config, slack_run, slack_response
):
    config = replace(config, slack_canvases=("F00000003",))
    reads = [channel(slack_response("read_channel.txt")), thread(slack_response("read_thread.txt"))]
    slack_run(config, T0, reads)
    report = build(config)
    assert "This run saved reads for 0 of 1 listed canvas(es)." in report
    assert "1 of 1 channel(s) moved" in report


def test_deleting_the_state_and_building_again_gives_the_same_start_points(
    config, first_export, slack_run, incremental
):
    slack_run(config, T1, incremental)
    build(config)
    points = read_state(config.data_dir)
    state_path(config.data_dir).unlink()
    assert f"{CHANNEL}: from the start" in begin(config, [PAIR], now=T1 + timedelta(hours=1))
    build(config)
    assert read_state(config.data_dir) == points == {CHANNEL: "2026-10-06T12:00:00Z"}


@pytest.mark.parametrize("damage", ["not JSON", "[]", '{"C00000001": "yesterday"}'])
def test_a_damaged_state_file_means_the_next_run_starts_from_the_beginning(
    config, first_export, damage
):
    state_path(config.data_dir).write_text(damage, encoding="utf-8")
    assert f"{CHANNEL}: from the start" in begin(config, [PAIR], now=T1)


def test_a_run_from_before_incremental_exports_counts_as_a_complete_first_export(
    config, slack_hook, slack_response
):
    # Runs saved before this change had a `verified` check mark in run.json, and channel reads
    # with no start: a full run, so the next run is already incremental.
    begin(config, [PAIR], now=T0)
    run = json.loads(run_path(config.data_dir).read_text(encoding="utf-8"))
    run_path(config.data_dir).write_text(json.dumps(run | {"verified": [CHANNEL]}), "utf-8")
    reads = [channel(slack_response("read_channel.txt")), thread(slack_response("read_thread.txt"))]
    for i, (tool, tool_input, response) in enumerate(reads, start=1):
        payload = slack_hook(tool, tool_input, response, f"toolu_old{i}")
        assert save(config, payload, now=T0 + timedelta(minutes=i)) == 0
    build(config)
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-04T12:00:00Z"}


def test_a_full_run_starts_every_channel_from_the_beginning(
    config, first_export, slack_run, slack_response
):
    reads = [channel(slack_response("read_channel.txt")), thread(slack_response("read_thread.txt"))]
    report = slack_run(config, T1, reads, full=True)
    assert "This is a full run" in report and f"{CHANNEL}: from the start" in report
    assert open_run(config.data_dir).full
    assert "1 of 1 channel(s) moved" in build(config)
    assert read_state(config.data_dir) == {CHANNEL: "2026-10-06T12:00:00Z"}
