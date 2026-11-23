"""People who joined a channel after its notice are left out like opted-out people (#118).
Synthetic fixtures only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata.common.config import ConfigError, load_config
from diffdata.discover.slack_export import build
from diffdata.discover.slack_inbox import SlackExportError, begin, save
from diffdata.scrub.run import ScrubError, scrub

CHANNEL = "C00000001"
PARENT = "1790864040.000100"
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SLACK_FIXTURES = Path(__file__).parent / "fixtures" / "slack"
# The notice went up on Oct 1, between the kickoff post and the rest.
NOTICE = "https://example.slack.com/archives/C00000001/p1790900000000000"
NOTICED = f"{CHANNEL}/1790900000.000000"
LATE = "U00000009"  # Jordan joins after the notice
EARLY = "U00000002"  # Sam's join is before it


def message(name: str, user: str, ts: str, text: str) -> str:
    return (
        f"=== Message from {name} <{name.split()[0].lower()}@example.com> ({user}) at "
        f"2026-10-02 12:00:00 CDT ===\nMessage TS: {ts}\n{text}\n\n"
    )


def joined(name: str, user: str, ts: str) -> str:
    return message(name, user, ts, f"<@{user}|{name}> has joined the channel")


@pytest.fixture
def channel_read(tmp_path):
    """The usual channel read, with joins and a late joiner's post added at the top."""

    def make(*extra: str) -> str:
        original = (SLACK_FIXTURES / "read_channel.txt").read_text(encoding="utf-8")
        head, _, rest = original.partition("\n\n")
        path = tmp_path / f"read_channel_joins_{len(list(tmp_path.glob('read_*')))}.txt"
        path.write_text(head + "\n\n" + "".join(extra) + rest, encoding="utf-8")
        return str(path)

    return make


@pytest.fixture
def config(tmp_path):
    from diffdata.common.config import Config

    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_notices=(("example-channel", NOTICED),),
        slack_workspace_url="https://example.slack.com",
    )


@pytest.fixture
def export_run(config, slack_response, slack_hook):
    def run(channel_fixture, settings=None, when=T0):
        begin(config, [f"example-channel={CHANNEL}"], now=when)
        reads = [
            ("slack_read_channel", {"channel_id": CHANNEL}, channel_fixture),
            ("slack_read_thread", {"channel_id": CHANNEL, "message_ts": PARENT}, "read_thread.txt"),
        ]
        for i, (tool, tool_input, fixture) in enumerate(reads, start=1):
            payload = slack_hook(
                tool, tool_input, slack_response(fixture), f"toolu_{when:%d%H%M}{i}"
            )
            assert save(config, payload, now=when + timedelta(minutes=i)) == 0
        return build(settings or config)

    return run


def store(config) -> list[dict]:
    path = config.data_dir / "slack" / "store" / CHANNEL / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def everything_built(config) -> str:
    slack = config.data_dir / "slack"
    paths = [*(slack / "store").rglob("*.jsonl"), *(slack / "export").rglob("*.md")]
    paths += [*(config.data_dir / "references").glob("*")]
    return "\n".join(p.read_text(encoding="utf-8") for p in paths if p.is_file())


JORDAN_JOINS = joined("Jordan Join", LATE, "1790960000.000390")
JORDAN_POSTS = message("Jordan Join", LATE, "1790990000.000450", "Jordan's late message")
SAM_JOINED_EARLY = joined("Sam Sample", EARLY, "1790800000.000050")


def test_someone_who_joined_after_the_notice_is_left_out_of_everything_built(
    config, export_run, channel_read
):
    report = export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS, SAM_JOINED_EARLY))
    built = everything_built(config)
    assert "Jordan's late message" not in built and "Jordan Join" not in built
    assert all(r["user_id"] != LATE for r in store(config))
    assert any(r["user_id"] == EARLY for r in store(config))  # joined before: stays
    assert "1 person(s) joined a channel after its notice, so their 2 message(s)" in report
    assert LATE not in report and "Jordan" not in report  # counts only


def test_a_late_joiners_names_are_kept_for_the_scrub(config, export_run, channel_read):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    left_out = json.loads((config.data_dir / "slack" / "left_out.json").read_text("utf-8"))
    assert left_out[LATE] == ["Jordan Join"]


@pytest.mark.parametrize("early_first", [False, True])
def test_someone_who_left_and_joined_again_after_the_notice_is_left_out(
    config, export_run, channel_read, early_first
):
    rejoined = joined("Sam Sample", EARLY, "1790995000.000460")
    order = (SAM_JOINED_EARLY, rejoined) if early_first else (rejoined, SAM_JOINED_EARLY)
    export_run(channel_read(*order))
    assert all(r["user_id"] != EARLY for r in store(config))


def test_must_refuse_a_typed_join_line_doesnt_make_someone_else_a_late_joiner(
    config, export_run, channel_read
):
    forged = message(
        "Alex Example",
        "U00000001",
        "1790961000.000391",
        f"<@{LATE}|Jordan Join> has joined the channel",
    )
    export_run(channel_read(JORDAN_POSTS, forged))
    assert any(r["user_id"] == LATE for r in store(config))
    assert any(r["user_id"] == "U00000001" for r in store(config))  # nor its author


def test_someone_with_no_join_message_isnt_a_late_joiner(config, export_run, channel_read):
    export_run(channel_read(JORDAN_POSTS))  # no join in the channel itself
    assert any(r["user_id"] == LATE for r in store(config))


def test_must_refuse_a_channel_with_no_notice_entry_stops_the_build(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    before = everything_built(config)
    with pytest.raises(SlackExportError, match="no entry for channel 1 in slack_channels") as e:
        build(replace(config, slack_notices=()))
    assert "example-channel" not in str(e.value)
    assert everything_built(config) == before


def test_must_refuse_a_notice_link_to_another_channel_stops_the_build(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    before = everything_built(config)
    other = (("example-channel", "C00000099/1790900000.000000"),)
    with pytest.raises(SlackExportError, match="points to a different channel") as e:
        build(replace(config, slack_notices=other))
    assert "C00000099" not in str(e.value)
    assert everything_built(config) == before


def test_none_turns_the_check_off_and_says_so_by_count(config, export_run, channel_read):
    report = export_run(
        channel_read(JORDAN_POSTS, JORDAN_JOINS),
        settings=replace(config, slack_notices=(("example-channel", "none"),)),
    )
    assert any(r["user_id"] == LATE for r in store(config))
    assert "1 channel(s) not checked for people who joined after the notice" in report
    assert "example-channel" not in report


def test_must_refuse_the_scrub_waits_for_a_build_after_the_notice_changes(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    later = (("example-channel", f"{CHANNEL}/1791000000.000000"),)  # Jordan now in
    with pytest.raises(ScrubError, match="has changed since the last build"):
        scrub(replace(config, slack_notices=later))
    assert not (config.data_dir / "scrubbed").exists()


def test_must_refuse_the_scrub_waits_for_a_build_when_someone_becomes_a_late_joiner(
    config, export_run, channel_read
):
    later = (("example-channel", f"{CHANNEL}/1791000000.000000"),)  # Jordan in at first
    export_run(
        channel_read(JORDAN_POSTS, JORDAN_JOINS), settings=replace(config, slack_notices=later)
    )
    with pytest.raises(ScrubError, match="has changed since the last build"):
        scrub(config)  # the notice is earlier now, so Jordan joined after it
    assert not (config.data_dir / "scrubbed").exists()


def test_a_build_that_left_out_a_late_joiner_lets_the_scrub_run(config, export_run, channel_read):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    assert "Slack day file(s)" in scrub(config)
    copied = "".join(
        p.read_text(encoding="utf-8") for p in (config.data_dir / "scrubbed").rglob("*.md")
    )
    assert "Jordan" not in copied


def test_must_refuse_the_scrub_stops_on_a_late_joiners_line_in_the_store(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    path = config.data_dir / "slack" / "store" / CHANNEL / "messages.jsonl"
    line = dict(store(config)[0], user_id=LATE, ts="1790990001.000451")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    with pytest.raises(ScrubError, match="joined a channel after its notice"):
        scrub(config)
    assert not (config.data_dir / "scrubbed").exists()


def test_with_no_late_joiners_the_stamp_matches_an_opt_out_only_build(
    config, export_run, channel_read
):
    from diffdata.discover.slack_inbox import applied_stamp, opt_out_stamp

    export_run(channel_read(SAM_JOINED_EARLY))
    assert applied_stamp(config.data_dir) == opt_out_stamp([])


# Reading the setting from config.toml


def write(tmp_path, notices: str, channels='["example-channel"]') -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"[local]\ndata_dir = '{(tmp_path / 'data').as_posix()}'\n\n[sources]\n"
        f"slack_channels = {channels}\n{notices}\n",
        encoding="utf-8",
    )
    return path


def test_a_notice_link_is_read_as_its_channel_and_ts(tmp_path):
    config = load_config(write(tmp_path, f'slack_notices = {{ "example-channel" = "{NOTICE}" }}'))
    assert config.slack_notices == (("example-channel", NOTICED),)


def test_none_is_read(tmp_path):
    config = load_config(write(tmp_path, 'slack_notices = { "#Example-Channel" = "None" }'))
    assert config.slack_notices == (("example-channel", "none"),)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.slack.com/docs/T00000001/F00000002",  # a canvas link
        f"{NOTICE}?thread_ts=1790900000.000000",  # a reply in a thread
        "1790900000.000000",  # a bare ts
        "",
    ],
)
def test_must_refuse_a_notice_that_isnt_a_message_link(tmp_path, value):
    with pytest.raises(ConfigError, match="isn't a link to a Slack message") as e:
        load_config(write(tmp_path, f'slack_notices = {{ "example-channel" = "{value}" }}'))
    assert value not in str(e.value) or not value


def test_must_refuse_a_notice_for_a_channel_that_isnt_listed(tmp_path):
    with pytest.raises(ConfigError, match="isn't in slack_channels"):
        load_config(write(tmp_path, f'slack_notices = {{ "exampel-channel" = "{NOTICE}" }}'))


def test_must_refuse_notices_that_arent_a_table(tmp_path):
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(write(tmp_path, f'slack_notices = ["{NOTICE}"]'))


def test_must_refuse_an_export_of_a_channel_with_no_notice_entry_stops_before_any_read(config):
    with pytest.raises(SlackExportError, match="no entry in slack_notices"):
        begin(replace(config, slack_notices=()), [f"example-channel={CHANNEL}"], now=T0)
    assert not (config.data_dir / "slack" / "run.json").exists()
