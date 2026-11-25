"""Who joined a channel after its notice (#118, CON-5).

Under the sign-up step, everyone in a signed-up channel gets the pinned notice and 7 days to
opt out. Someone who joins after the notice went up never got either, so their messages in
that channel are left out, exactly like those of someone in `slack_opted_out`.

Slack shows each join in the channel's history as a message from the person who joined:
`<@U...|Name> has joined the channel`. A person is a late joiner in a channel if their latest
such message there is after the channel's notice, so someone who left and came back after it
counts too. The line only counts when its author is the person it names, so nobody can make
someone else a late joiner by typing it. Someone with no join message (in the channel before
its visible history starts, or its creator) isn't one.

The joins come from the channel reads saved in the inbox, which every build reads in full,
so a join saved by an earlier run still counts. A join is only seen if a read reached back
to it, so the list is trusted only up to the start of the channel's newest complete run
(slack_state.py), which read everything up to then. Messages newer than that, or newer than
the notice if no run has been complete yet, are held back until a complete run covers them:
a test run, or a read cut short, could have saved someone's posts without their join.

Each channel being built needs an entry in `slack_notices`: its notice link, or "none" on
purpose. The save hook doesn't use this: a channel read comes newest first, so it sees a
late joiner's messages before their join. Their raw messages stay in the producer's data
folder, but never reach the store, the day files, the reference log or the scrubbed copies.
"""

import re

from diffdata.common.config import NO_NOTICE, Config, ConfigError
from diffdata.discover.slack_inbox import SlackExportError, opt_out_stamp
from diffdata.discover.slack_state import planned_start

JOIN = re.compile(r"<@([UW][A-Z0-9]{8,})(?:\|[^>\n]*)?> has joined the channel")


def ts_key(ts: str) -> tuple[int, int]:
    seconds, _, fraction = ts.partition(".")
    return int(seconds), int(fraction or 0)


def joins(records: list[dict]) -> dict[str, str]:
    """Each person's latest join in these records: user ID to the join's ts."""
    latest: dict[str, str] = {}
    for record in records:
        found = JOIN.fullmatch(record["text"].strip())
        if record.get("thread_ts") or not found or found[1] != record["user_id"]:
            continue
        user_id = found[1]  # the author, since the two must match
        if user_id not in latest or ts_key(record["ts"]) > ts_key(latest[user_id]):
            latest[user_id] = record["ts"]
    return latest


def notice_for(
    config: Config, channel_id: str, name: str | None, has_reads: bool = True
) -> str | None:
    """The ts of this channel's notice, or None when it's set to "none". Stops the build when
    there's no entry, the link is another channel's, or the channel has a notice but no saved
    reads to find its joins in."""
    # Channels are named by their place in slack_channels: output may be shared (DIS-1).
    # A Config made in code can leave listed_channels empty.
    listed = list(config.listed_channels or config.slack_channels + config.slack_channel_ids)
    place = (
        listed.index(channel_id)
        if channel_id in listed
        else (listed.index(name) if name in listed else None)
    )
    shown = (
        f"channel {place + 1} in slack_channels"
        if place is not None
        else "a channel in the saved data that isn't in slack_channels"
    )
    try:
        value = config.notice(channel_id, name)
    except ConfigError as e:
        raise SlackExportError(f"{e} Nothing was built or scrubbed.") from e
    if value is None:
        raise SlackExportError(
            f"slack_notices in config.toml has no entry for {shown}, so nothing was built or "
            'scrubbed. Add the link to the channel\'s pinned notice (Copy link), or "none" if '
            "the channel has no notice because everyone in it agreed directly. Key it by the "
            "channel's name or ID, as slack_channels lists it."
        )
    if value == NO_NOTICE:
        return None
    linked_channel, _, ts = value.partition("/")
    if not has_reads:
        raise SlackExportError(
            f"The saved Slack reads for {shown} are missing from the inbox folder in your data "
            "folder, so who joined after its notice can't be checked. Nothing was built or "
            "scrubbed. Put the inbox folder back, or run /slack-export full to read the "
            "channel again."
        )
    if linked_channel != channel_id:
        raise SlackExportError(
            f"The slack_notices link for {shown} in config.toml points to a different channel, "
            "so nothing was built or scrubbed. Use Copy link on the notice pinned in that "
            "channel."
        )
    return ts


def late_joiners(
    config: Config,
    records: dict[str, list[dict] | None],
    names: dict[str, str],
    points: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, tuple[int, int]], list[str]]:
    """For each channel in `records` (channel ID to its saved messages): the people who joined
    after its notice, and the ts after which its messages are held back, because no complete
    run has read that far yet (`points`: each channel's newest complete run, slack_state.py).
    Also returns the IDs of channels set to "none", for the report."""
    late: dict[str, set[str]] = {}
    held: dict[str, tuple[int, int]] = {}
    unchecked: list[str] = []
    for channel_id, messages in sorted(records.items()):
        name = names.get(channel_id)
        notice = notice_for(config, channel_id, name, has_reads=messages is not None)
        if notice is None:
            unchecked.append(channel_id)
            late[channel_id] = set()
            continue
        late[channel_id] = {
            user_id for user_id, ts in joins(messages).items() if ts_key(ts) > ts_key(notice)
        }
        # The newest complete run's start, less the same overlap the next run reads back by,
        # since the start time comes from this computer's clock (slack_state.py).
        covered = planned_start(points.get(channel_id))
        held[channel_id] = max(ts_key(notice), (covered, 0) if covered is not None else (0, 0))
    return late, held, unchecked


def left_out_stamp(
    opted_out: set[str], late: dict[str, set[str]], held: dict[str, tuple[int, int]]
) -> str:
    """A fingerprint of everything left out: the opt-out list, each channel's late joiners and
    where its held-back messages start. The held-back point can move earlier with no export,
    as when a notice link is corrected, and a build cut short after the store but before the
    day files would then leave copies the scrub must not take. With every channel set to
    "none" it's the opt-out list's own stamp, so earlier builds still match."""
    return opt_out_stamp(
        list(opted_out)
        + [f"{channel}/{user}" for channel, users in late.items() for user in users]
        + [f"{channel}@{after[0]}.{after[1]:06d}" for channel, after in held.items()]
    )


def is_held(record: dict, held: dict[str, tuple[int, int]], channel_id: str) -> bool:
    """Whether a message is newer than what complete runs have read in its channel."""
    return channel_id in held and ts_key(record["ts"]) > held[channel_id]
