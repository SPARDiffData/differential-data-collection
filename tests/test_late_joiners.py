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
    with pytest.raises(ScrubError, match="still has messages the build leaves out"):
        scrub(config)
    assert not (config.data_dir / "scrubbed").exists()


def test_a_channel_set_to_none_keeps_the_opt_out_lists_own_stamp(config, export_run, channel_read):
    from diffdata.discover.slack_inbox import applied_stamp, opt_out_stamp

    export_run(
        channel_read(SAM_JOINED_EARLY),
        settings=replace(config, slack_notices=(("example-channel", "none"),)),
    )
    assert applied_stamp(config.data_dir) == opt_out_stamp([])  # existing data still matches


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


def test_must_refuse_a_misspelt_notice_entry_leaves_its_channel_without_one(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    two = replace(
        config,
        slack_channels=("example-channel", "other-channel"),
        slack_notices=(("exampel-channel", NOTICED), ("other-channel", "none")),
    )
    with pytest.raises(SlackExportError, match="no entry for channel 1 in slack_channels"):
        build(two)


def test_a_channel_taken_off_slack_channels_still_builds_with_its_entry(
    config, export_run, channel_read
):
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    build(replace(config, slack_channels=("other-channel",)))
    assert all(r["user_id"] != LATE for r in store(config))
    with pytest.raises(SlackExportError, match="that isn't in slack_channels"):
        build(replace(config, slack_channels=("other-channel",), slack_notices=()))


def test_an_entry_for_a_channel_not_in_slack_channels_is_read(tmp_path):
    config = load_config(write(tmp_path, f'slack_notices = {{ "old-channel" = "{NOTICE}" }}'))
    assert config.slack_notices == (("old-channel", NOTICED),)


def test_an_enterprise_grid_link_is_read(tmp_path):
    link = NOTICE.replace("example.slack.com", "example-org.enterprise.slack.com")
    config = load_config(write(tmp_path, f'slack_notices = {{ "example-channel" = "{link}" }}'))
    assert config.slack_notices == (("example-channel", NOTICED),)


def test_must_refuse_notices_that_arent_a_table(tmp_path):
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(write(tmp_path, f'slack_notices = ["{NOTICE}"]'))


def test_must_refuse_an_export_of_a_channel_with_no_notice_entry_stops_before_any_read(config):
    with pytest.raises(SlackExportError, match="no entry in slack_notices"):
        begin(replace(config, slack_notices=()), [f"example-channel={CHANNEL}"], now=T0)
    assert not (config.data_dir / "slack" / "run.json").exists()


def test_must_refuse_an_export_whose_notice_link_is_another_channels(config):
    other = (("example-channel", "C00000099/1790900000.000000"),)
    with pytest.raises(SlackExportError, match="points to a different channel"):
        begin(replace(config, slack_notices=other), [f"example-channel={CHANNEL}"], now=T0)
    assert not (config.data_dir / "slack" / "run.json").exists()


# Reads that don't reach back to a join


@pytest.fixture
def short_read(config, slack_response, slack_hook):
    """A run that reads the channel but not its thread, so it isn't complete: like a test
    run, or a read cut short."""

    def run(channel_fixture, when=T0):
        begin(config, [f"example-channel={CHANNEL}"], now=when)
        payload = slack_hook(
            "slack_read_channel",
            {"channel_id": CHANNEL},
            slack_response(channel_fixture),
            f"toolu_{when:%d%H%M}1",
        )
        assert save(config, payload, now=when + timedelta(minutes=1)) == 0
        return build(config)

    return run


def test_must_refuse_a_read_that_misses_the_join_holds_back_everything_after_the_notice(
    config, short_read, channel_read
):
    report = short_read(channel_read(JORDAN_POSTS))  # Jordan's post, but not his join
    assert "Jordan" not in everything_built(config)
    kept = store(config)
    assert kept and all(r["ts"] <= "1790900000.000000" for r in kept)  # before the notice
    assert "message(s) held back" in report
    assert "Slack day file(s)" in scrub(config)
    copied = "".join(
        p.read_text(encoding="utf-8") for p in (config.data_dir / "scrubbed").rglob("*.md")
    )
    assert "Jordan" not in copied


def test_a_complete_run_later_adds_the_held_back_messages_and_finds_the_late_joiner(
    config, short_read, export_run, channel_read
):
    short_read(channel_read(JORDAN_POSTS))
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS), when=T0 + timedelta(days=1))
    assert all(r["user_id"] != LATE for r in store(config))
    assert any(r["ts"] == "1791021600.000500" for r in store(config))  # Alex's Oct 3 post


def test_must_refuse_a_test_run_after_a_complete_one_holds_back_only_whats_newer(
    config, export_run, short_read, channel_read
):
    export_run(channel_read(SAM_JOINED_EARLY))
    newer = message("Jordan Join", LATE, "1791200000.000600", "Jordan's newer message")
    report = short_read(channel_read(newer), when=T0 + timedelta(days=3))
    assert "Jordan" not in everything_built(config)
    assert any(r["ts"] == "1791021600.000500" for r in store(config))  # covered: kept
    assert "1 message(s) held back" in report


def test_must_refuse_the_scrub_stops_on_a_held_back_line_in_the_store(
    config, short_read, channel_read
):
    short_read(channel_read(JORDAN_POSTS))
    path = config.data_dir / "slack" / "store" / CHANNEL / "messages.jsonl"
    line = dict(store(config)[0], ts="1790990002.000452")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    with pytest.raises(ScrubError, match="newer than the last complete export"):
        scrub(config)


def test_must_refuse_the_scrub_after_a_build_cut_short_once_the_notice_moved_earlier(
    config, slack_response, slack_hook, channel_read, monkeypatch
):
    from diffdata.discover import slack_export

    too_late = replace(config, slack_notices=(("example-channel", f"{CHANNEL}/1791000000.000000"),))
    begin(too_late, [f"example-channel={CHANNEL}"], now=T0)
    read = slack_response(channel_read(JORDAN_POSTS))  # his post, not his join
    payload = slack_hook("slack_read_channel", {"channel_id": CHANNEL}, read, "toolu_041200")
    assert save(too_late, payload, now=T0 + timedelta(minutes=1)) == 0
    build(too_late)  # no complete run: held back only after the (wrong) notice
    assert "Jordan's late message" in everything_built(config)

    def cut_short(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(slack_export, "write_view", cut_short)
    with pytest.raises(OSError):
        build(config)  # the corrected, earlier notice: the store is cleaned, the days aren't
    with pytest.raises(ScrubError, match="has changed since the last build"):
        scrub(config)
    assert not (config.data_dir / "scrubbed").exists()


def test_a_message_just_before_the_last_complete_runs_start_is_still_held_back(
    config, export_run, short_read, channel_read
):
    export_run(channel_read(SAM_JOINED_EARLY))  # complete, begun at T0 by this computer's clock
    just_before = str(int(T0.timestamp()) - 1800) + ".000700"  # 30 minutes before T0
    late_post = message("Jordan Join", LATE, just_before, "Jordan's post near the run")
    report = short_read(channel_read(late_post), when=T0 + timedelta(days=3))
    assert "Jordan" not in everything_built(config)
    assert "1 message(s) held back" in report


# Merged with #131: slack_channels can list a channel by its ID or a link, so slack_notices
# can key its entry the same way.


def test_a_notice_entry_can_be_keyed_by_the_channels_id_or_a_link(tmp_path):
    by_id = load_config(write(tmp_path, f'slack_notices = {{ {CHANNEL} = "{NOTICE}" }}'))
    assert by_id.slack_notices == ((CHANNEL, NOTICED),)
    link = f"https://example.slack.com/archives/{CHANNEL}"
    by_link = load_config(write(tmp_path, f'slack_notices = {{ "{link}" = "none" }}'))
    assert by_link.slack_notices == ((CHANNEL, "none"),)


def test_a_bad_notice_keyed_by_id_never_prints_the_id(tmp_path):
    with pytest.raises(ConfigError, match="slack_notices entry 1 in") as e:
        load_config(write(tmp_path, f'slack_notices = {{ {CHANNEL} = "not a link" }}'))
    assert CHANNEL not in str(e.value)


def test_a_channel_listed_by_id_uses_its_id_keyed_notice(config, export_run, channel_read):
    by_id = replace(
        config,
        slack_channels=(),
        slack_channel_ids=(CHANNEL,),
        listed_channels=(CHANNEL,),
        slack_notices=((CHANNEL, NOTICED),),
    )
    report = export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS), settings=by_id)
    assert all(r["user_id"] != LATE for r in store(by_id))
    assert "1 person(s) joined a channel after its notice" in report


def test_begin_takes_a_bare_id_with_an_id_keyed_notice(config):
    by_id = replace(config, slack_channels=(), slack_channel_ids=(CHANNEL,))
    begin(replace(by_id, slack_notices=((CHANNEL, NOTICED),)), [CHANNEL], now=T0)


def test_must_refuse_begin_for_a_bare_id_with_only_a_name_keyed_notice_and_no_name_yet(config):
    # Before its first read, a channel listed by ID has no name, so a name-keyed entry can't
    # be matched to it. begin stops before any read, without printing the ID.
    by_id = replace(config, slack_channels=(), slack_channel_ids=(CHANNEL,))
    with pytest.raises(SlackExportError, match="key the entry by that ID") as e:
        begin(by_id, [CHANNEL], now=T0)
    assert CHANNEL not in str(e.value)


def test_must_refuse_two_different_notice_entries_for_one_channel(config, export_run, channel_read):
    both = replace(config, slack_notices=(("example-channel", NOTICED), (CHANNEL, "none")))
    with pytest.raises(SlackExportError, match="two different entries"):
        begin(both, [f"example-channel={CHANNEL}"], now=T0)
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS))
    with pytest.raises(SlackExportError, match="two different entries"):
        build(both)
    with pytest.raises(ScrubError, match="two different entries"):
        scrub(both)


def test_the_same_notice_keyed_by_name_and_id_is_fine(config, export_run, channel_read):
    both = replace(config, slack_notices=(("example-channel", NOTICED), (CHANNEL, NOTICED)))
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS), settings=both)
    assert all(r["user_id"] != LATE for r in store(both))


# Codex's review of the merge.


def test_a_link_key_that_isnt_a_channel_link_is_refused_and_never_printed(tmp_path):
    key = "https://example.enterprise.slack.com/archives/C00000001"
    with pytest.raises(ConfigError, match="a link, but not to a Slack channel") as e:
        load_config(write(tmp_path, f'slack_notices = {{ "{key}" = "none" }}'))
    assert key not in str(e.value)


def test_a_bad_notice_under_an_odd_key_never_prints_the_key(tmp_path):
    # A made-up token shape, built from parts so the repo never holds one whole.
    key = "xo" + "xb-1111-2222-fakefakefake"
    toml = f'slack_notices = {{ "example-channel" = "none", "{key}" = "not a link" }}'
    with pytest.raises(ConfigError, match="slack_notices entry 2 in") as e:
        load_config(write(tmp_path, toml))
    assert key not in str(e.value)


def test_must_refuse_begin_borrowing_another_channels_entry_by_a_given_name(config):
    # C00000001 is listed by ID. A name given to begin isn't how config.toml lists it, so it
    # can't pick up the "none" entry of a channel with that name.
    for listed in [(), ("other-channel",)]:  # even when another channel is listed by it
        by_id = replace(
            config,
            slack_channels=listed,
            slack_channel_ids=(CHANNEL,),
            slack_notices=(("other-channel", "none"),),
        )
        with pytest.raises(SlackExportError, match="no entry in slack_notices"):
            begin(by_id, [f"other-channel={CHANNEL}"], now=T0)


def test_must_refuse_a_store_whose_inbox_is_gone_is_still_checked_against_its_notice(
    config, export_run, channel_read
):
    unchecked = replace(config, slack_notices=(("example-channel", "none"),))
    export_run(channel_read(JORDAN_POSTS, JORDAN_JOINS), settings=unchecked)
    assert any(r["user_id"] == LATE for r in store(config))  # "none": not checked
    import shutil

    shutil.rmtree(config.data_dir / "slack" / "inbox" / CHANNEL)
    build(unchecked)  # still "none": nothing to check, so it builds
    # A notice is added now. With no saved reads, no join can be seen: both stop.
    with pytest.raises(SlackExportError, match="reads for channel 1 in slack_channels are miss"):
        build(config)
    with pytest.raises(ScrubError, match="are missing") as e:
        scrub(config)
    assert CHANNEL not in str(e.value) and "example-channel" not in str(e.value)
    assert any(r["user_id"] == LATE for r in store(config))  # the build changed nothing
