"""Building the Slack store and markdown view from the inbox (DIS-1). Synthetic fixtures only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import __version__
from diffdata.common.config import Config
from diffdata.discover.slack_export import build
from diffdata.discover.slack_inbox import SlackExportError, begin, save

CHANNEL = "C00000001"
PARENT = "1790864040.000100"
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SLACK_FIXTURES = Path(__file__).parent / "fixtures" / "slack"


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
    )


@pytest.fixture
def export_run(config, slack_response, slack_hook):
    """One /slack-export run as the hook saves it, then the build: a channel read, then
    a read of the one thread with replies."""

    def run(when=T0, channel_fixture="read_channel.txt"):
        begin(config, ["example-channel=C00000001"], now=when)
        reads = [
            ("slack_read_channel", {"channel_id": CHANNEL}, channel_fixture),
            ("slack_read_thread", {"channel_id": CHANNEL, "message_ts": PARENT}, "read_thread.txt"),
        ]
        for i, (tool, tool_input, fixture) in enumerate(reads, start=1):
            payload = slack_hook(
                tool, tool_input, slack_response(fixture), f"toolu_{when:%d%H%M}{i}"
            )
            assert save(config, payload, now=when + timedelta(minutes=i)) == 0
        return build(config)

    return run


def view(config):
    folder = config.data_dir / "slack" / "export" / "example-channel"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md"))}


def store_lines(config):
    path = config.data_dir / "slack" / "store" / CHANNEL / "messages.jsonl"
    return path.read_text(encoding="utf-8").splitlines()


def test_the_view_matches_the_expected_markdown(config, export_run):
    export_run()
    expected = {
        p.name: p.read_text(encoding="utf-8") for p in (SLACK_FIXTURES / "expected").glob("*.md")
    }
    actual = {
        name: text.replace(f"diffdata {__version__}", "diffdata VERSION")
        for name, text in view(config).items()
    }
    assert actual == expected


def test_the_report_gives_counts_only(export_run):
    report = export_run()
    assert report.splitlines()[0] == (
        "1 channel(s): 2 result file(s), 9 message(s) read, 8 new or changed, "
        "3 day file(s) written."
    )
    for private in ("Alex", "example-channel", CHANNEL, "Kickoff", "example.com"):
        assert private not in report


def test_a_late_reply_lands_under_its_parent(config, export_run):
    export_run()
    days = view(config)
    assert "One more thought on this, @Sam Sample." in days["2026-10-01.md"]
    assert "One more thought" not in days["2026-10-03.md"]


def test_building_again_adds_nothing(config, export_run):
    export_run()
    before = (store_lines(config), view(config))
    assert "0 new or changed, 0 day file(s) written" in build(config)
    assert (store_lines(config), view(config)) == before


def test_the_same_results_in_a_later_run_add_nothing(config, export_run):
    export_run()
    lines = store_lines(config)
    assert "0 new or changed" in export_run(when=T0 + timedelta(days=1))
    assert store_lines(config) == lines


def test_an_edited_message_keeps_both_versions(config, export_run, tmp_path):
    export_run()
    original = (SLACK_FIXTURES / "read_channel.txt").read_text(encoding="utf-8")
    edited = original.replace("11:30 PT on Monday.", "11:00 PT on Monday.")
    edited_fixture = tmp_path / "read_channel_edited.txt"  # a copy outside the fixtures
    edited_fixture.write_text(edited, encoding="utf-8")

    assert "1 new or changed" in export_run(T0 + timedelta(days=1), str(edited_fixture))
    versions = [json.loads(line) for line in store_lines(config)]
    texts = [v["text"] for v in versions if v["ts"] == "1791021600.000500"]
    assert texts == [
        "Meeting moved to 11:30 PT on Monday. <!here>",
        "Meeting moved to 11:00 PT on Monday. <!here>",
    ]
    day = view(config)["2026-10-03.md"]
    assert "11:00 PT" in day and "11:30 PT" not in day and " · edited" in day


# Sam's thread reply, as the channel read shows it when it was also sent to the channel.
BROADCAST = (
    "=== Message from Sam Sample <sam@example.com> (U00000002) at 2026-10-01 09:20:00 CDT ===\n"
    "Message TS: 1790864400.000110\n"
    "Added my comments, <@U00000001|Alex Example>. The plan canvas is here: "
    "<https://example.slack.com/docs/T00000000/F00000002>\n\n"
)


@pytest.mark.parametrize("thread_copy_first", [False, True])
def test_a_reply_also_sent_to_the_channel_stays_in_its_thread(
    config, export_run, tmp_path, thread_copy_first
):
    channel_read = (SLACK_FIXTURES / "read_channel.txt").read_text(encoding="utf-8")
    kickoff = "=== Message from Alex Example <alex@example.com> (U00000001) at 2026-10-01 09:14"
    with_broadcast = tmp_path / "read_channel_broadcast.txt"  # a copy outside the fixtures
    with_broadcast.write_text(channel_read.replace(kickoff, BROADCAST + kickoff), encoding="utf-8")

    if thread_copy_first:  # an earlier run stored the thread's copy before the channel's
        export_run()
        export_run(T0 + timedelta(days=1), str(with_broadcast))
    else:
        export_run(channel_fixture=str(with_broadcast))
    day = view(config)["2026-10-01.md"]
    assert "> **Sam Sample** · 14:20 UTC" in day
    assert "\n**Sam Sample** · 14:20 UTC" not in day  # not also shown as a post of its own
    assert "edited" not in day


SEARCH_TOOL = "slack_search_public_and_private"
MESSAGE_SEARCH = {
    "content_types": "messages",
    "filters": "in:<#C00000001>",
    "after": "1791111600",
    "channel_types": "public_channel,private_channel",
    "natural_language_query": "",
    "include_bots": True,
    "include_context": False,
    "sort": "timestamp",
    "limit": 20,
}


@pytest.fixture
def incremental_run(config, slack_run, slack_response, slack_search):
    """An incremental run two days after export_run's: the channel from its start point,
    with one new post, the search that finds a late reply in the older thread, and that
    thread read again in full."""

    def run():
        reads = [
            (
                "slack_read_channel",
                {"channel_id": CHANNEL, "oldest": "1791111600"},
                slack_response("read_channel_new.txt"),
            ),
            (SEARCH_TOOL, MESSAGE_SEARCH, slack_search()),
            (
                "slack_read_thread",
                {"channel_id": CHANNEL, "message_ts": PARENT},
                slack_response("read_thread_late.txt"),
            ),
        ]
        assert 'C00000001: from "1791111600"' in slack_run(config, T0 + timedelta(days=2), reads)
        return build(config)

    return run


def test_an_incremental_run_adds_only_whats_new(config, export_run, incremental_run):
    export_run()
    before = store_lines(config)
    report = incremental_run()
    # 4 result files: the message search's record isn't one, and holds no messages.
    assert report.splitlines()[0] == (
        "1 channel(s): 4 result file(s), 15 message(s) read, 2 new or changed, "
        "2 day file(s) written."
    )
    added = [json.loads(line) for line in store_lines(config)[len(before) :]]
    assert store_lines(config)[: len(before)] == before
    assert [r["ts"] for r in added] == ["1791212400.000600", "1791216000.000140"]
    assert "1 of 1 channel(s) moved their start point forward." in report
    for private in ("Sam", "example-channel", CHANNEL, "agenda", "example.com"):
        assert private not in report


def test_a_late_reply_found_by_search_lands_under_its_parent(config, export_run, incremental_run):
    export_run()
    incremental_run()
    days = view(config)
    assert "> **Sam Sample** · 2026-10-05 16:00 UTC" in days["2026-10-01.md"]
    assert "> One late reply: the summary is in the doc now." in days["2026-10-01.md"]
    assert "late reply" not in days["2026-10-05.md"]
    assert "Draft agenda for Friday" in days["2026-10-05.md"]


def test_building_after_an_incremental_run_again_adds_nothing(config, export_run, incremental_run):
    export_run()
    incremental_run()
    before = (store_lines(config), view(config))
    assert "0 new or changed, 0 day file(s) written" in build(config)
    assert (store_lines(config), view(config)) == before


def test_an_inbox_file_that_isnt_a_channel_or_thread_read_is_left_alone(config, export_run):
    export_run()
    stray = config.data_dir / "slack" / "inbox" / CHANNEL / "20261004T120000Z" / "other-1.json"
    stray.write_text(json.dumps({"tool_name": "something_else", "saved_at": "x"}), "utf-8")
    report = build(config)
    assert "2 result file(s)" in report and "Couldn't read" not in report


def test_the_store_keeps_ids_and_names_but_not_author_emails(config, export_run):
    export_run()
    records = [json.loads(line) for line in store_lines(config)]
    assert {r["user_id"] for r in records} == {"U00000001", "U00000002", "U00000003", "USLACKBOT"}
    store = "\n".join(store_lines(config))
    assert "alex@example.com" not in store and "sam@example.com" not in store


def test_everything_is_written_under_the_data_folder(config, export_run, tmp_path):
    export_run()
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(config.data_dir in p.parents for p in written)


def test_build_needs_the_workspace_address(tmp_path):
    with pytest.raises(SlackExportError, match="slack_workspace_url"):
        build(Config(data_dir=tmp_path / "data", slack_channels=("example-channel",)))


def test_a_malformed_message_ts_is_counted_and_the_build_finishes(config, export_run, tmp_path):
    channel_read = (SLACK_FIXTURES / "read_channel.txt").read_text(encoding="utf-8")
    broken = tmp_path / "read_channel_bad_ts.txt"  # a copy outside the fixtures
    broken.write_text(channel_read.replace("1790955177.000300", "1790955177.oops"), "utf-8")

    report = export_run(channel_fixture=str(broken))
    assert "8 message(s) read" in report
    assert "Couldn't read 1 message(s) and 0 result file(s)" in report
