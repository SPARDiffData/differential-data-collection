"""The 7-day window and the team's opt-out list, checked when an export starts (#132, decision
6, PR 3; CON-4, CON-5).

Each channel's notice gives its people 7 days to opt out through the shared form. When the
window closes, the project team looks at the answers and runs `diffdata admin optouts`, which
writes the team's opted-out Slack IDs to <folder>/_config/opted-out.json in the bucket. The
team's upload key can read that one folder.

`diffdata discover slack begin` (the start of every /slack-export) then:
  10  refuses to start while any channel in the run is inside its window: less than 7 days
      after the notice's time, which its link holds (slack_notices, #118);
  11  with a team folder under [store], fetches the team's list, and refuses to start if it
      can't: missing, unreadable, another team's, or written before the window of a channel in
      the run closed (so before the form's last answers could be in it). A missing list is
      never "nobody opted out". A channel with a notice needs a team folder to fetch from.
      With a team folder, the save hook, the build and the push refuse too while the saved
      list is missing or was fetched for another folder (require_saved);
  12  adds the fetched IDs to <data>/slack/team_opted_out.json, which only grows, and which
      load_config() adds to slack_opted_out. No one already in either is ever dropped.

Without a team folder under [store] there's no list to fetch: only channels set to "none" may
be exported, and only slack_opted_out applies. No ID is ever printed: reports give counts.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.common.config import (
    NO_NOTICE,
    TEAM_OPT_OUT_FORMAT,
    USER_ID,
    Config,
    read_team_opted_out,
    team_opted_out_path,
)

WINDOW = timedelta(days=7)
# Where the list sits, inside the team's folder. admin.py writes it there.
LIST_NAME = "_config/opted-out.json"
LIST_FORMAT = "diffdata-opted-out"
LIST_VERSION = 1
LIST_KEYS = {"format", "version", "team", "slack_opted_out", "tool"}
MAX_LIST_BYTES = 1_000_000

# (bucket, key, object name) -> (its bytes, when the bucket says it was written), or None if
# there's no such file. Any other failure raises StoreError.
Fetcher = Callable[[str, dict, str], tuple[bytes, datetime] | None]


class ListError(Exception):
    """The export can't start. The message names no person and quotes no ID."""


def window_closes(notice: str) -> datetime:
    """When a notice's 7 days end, to the microsecond. `notice` is slack_notices'
    "<channel ID>/<seconds>.<6 digits>"."""
    seconds, _, fraction = notice.partition("/")[2].partition(".")
    posted = datetime.fromtimestamp(int(seconds), UTC) + timedelta(microseconds=int(fraction or 0))
    return posted + WINDOW


def check_windows(notices: dict[str, str], now: datetime) -> datetime | None:
    """Item 10. `notices`: how each channel in the run is shown -> its slack_notices value.
    Returns when the last window closed, or None if no channel in the run has a notice."""
    closes = {shown: window_closes(n) for shown, n in notices.items() if n != NO_NOTICE}
    for shown, when in closes.items():
        if now < when:
            raise ListError(
                f"{shown}'s opt-out window is still open: its notice went up less than 7 days "
                f"ago. The export can start from {when:%Y-%m-%d %H:%M} UTC. Nothing was read."
            )
    return max(closes.values(), default=None)


def parse_list(raw: bytes, team: str) -> list[str]:
    """The IDs in a list `admin optouts` wrote for this team. Anything else is refused,
    without quoting it."""
    bad = (
        "The team's opt-out list in the store isn't one diffdata wrote for your team. Tell the "
        "project team. Nothing was read."
    )
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_LIST_BYTES else None
    except (UnicodeDecodeError, ValueError):
        raise ListError(bad) from None
    if not (
        isinstance(data, dict)
        and set(data) <= LIST_KEYS
        and data.get("format") == LIST_FORMAT
        and data.get("version") == LIST_VERSION
        and isinstance(data.get("slack_opted_out"), list)
    ):
        raise ListError(bad)
    if data.get("team") != team:
        raise ListError(
            "The opt-out list in your team's folder is for another team. Tell the project team. "
            "Nothing was read."
        )
    ids = data["slack_opted_out"]
    if not all(isinstance(i, str) and USER_ID.fullmatch(i) for i in ids) or len(set(ids)) != len(
        ids
    ):
        raise ListError(bad)
    return ids


def fetch_list(config: Config, closes: datetime | None, fetch: Fetcher | None = None) -> str:
    """Items 11 and 12: fetch the team's list and save it in the data folder. Returns a line
    for the report."""
    bucket, folder = config.store_bucket.strip(), config.store_folder.strip()
    if not bucket and not folder:
        if closes is not None:
            # A notice means the team's opt-out form is in use, so its answers must be applied.
            raise ListError(
                "A channel in this export has a notice, so your team's opt-out list must be "
                "applied, but there's no team folder (bucket and folder) under [store] in "
                "config.toml to fetch it from. Run diffdata setup with your team file. Nothing "
                "was read."
            )
        return (
            "No team folder under [store] in config.toml, so no team opt-out list was fetched: "
            'every channel in this export is set to "none", and only slack_opted_out applies.'
        )
    # Imported here: push imports the Slack inbox, which imports this module.
    from diffdata.store import push
    from diffdata.store.backend import StoreError

    if not push.bucket_name(bucket) or not push.SEGMENT.fullmatch(folder):
        raise ListError(
            "bucket and folder under [store] in config.toml must both be set, as the project "
            "team gave them, to fetch your team's opt-out list. Nothing was read."
        )
    try:
        push.check_environment()
        key = push.read_key(push.key_path(config))
    except push.PushError as e:
        raise ListError(
            str(e).replace(push.NOTHING_SENT, "")
            + "It's needed to fetch your team's opt-out list. Nothing was read."
        ) from None
    try:
        found = (fetch or gcs_fetch)(bucket, key, f"{folder}/{LIST_NAME}")
    except StoreError as e:
        raise ListError(
            f"Couldn't fetch your team's opt-out list, so the export can't start: {e} "
            "Nothing was read."
        ) from None
    if found is None:
        raise ListError(
            "Your team's opt-out list isn't in the store yet. The project team writes it after "
            "the 7 days, once they've looked at the form's answers. Ask them, then run again. "
            "Nothing was read."
        )
    raw, written = found
    ids = parse_list(raw, folder)
    if closes is not None and written < closes:
        raise ListError(
            "Your team's opt-out list was written before a channel's 7 days ended, so it may "
            "miss the last answers. Ask the project team to write it again. Nothing was read."
        )
    added = save_list(config.data_dir, bucket, folder, ids)
    return (
        f"Fetched your team's opt-out list: {len(ids)} person(s) on it, {len(added)} new since "
        "the last fetch. Their messages are left out like those in slack_opted_out."
    )


def gcs_fetch(bucket: str, key: dict, name: str) -> tuple[bytes, datetime] | None:
    from diffdata.store.gcs import fetch  # only here: Google's library loads for a real run

    return fetch(bucket, key, name)


def save_list(data: Path, bucket: str, folder: str, ids: list[str]) -> list[str]:
    """Add the fetched IDs to the saved list, under a lock, so two starts at once can't drop
    one another's. It never shrinks. Returns the IDs that are new."""
    from diffdata.discover.slack_inbox import file_locked, write_atomic

    path = team_opted_out_path(data)
    for place in (path.parent, path):
        if place.is_symlink() or place.is_junction():
            raise ListError(
                f"{place.name} in your data folder is a link, so your team's opt-out list could "
                "be written somewhere else. Replace it with a plain folder or file. Nothing was "
                "read."
            )
    with file_locked(path.with_name(f"{path.name}.lock"), f"slack/{path.name}"):
        saved = read_team_opted_out(data)
        added = [i for i in ids if i not in saved]
        body = {"format": TEAM_OPT_OUT_FORMAT, "bucket": bucket, "folder": folder}
        write_atomic(path, json.dumps({**body, "slack_opted_out": saved + added}) + "\n")
    return added


def require_saved(config: Config, step: str) -> None:
    """For the save hook, the build and the push: with a team folder under [store], a list
    fetched for that same folder must be in the data folder. Without it, opted-out people's
    messages would get back in. A damaged one stops too (read_team_opted_out)."""
    bucket, folder = config.store_bucket.strip(), config.store_folder.strip()
    if not bucket and not folder:
        return
    path = team_opted_out_path(config.data_dir)
    read_team_opted_out(config.data_dir)  # damaged: ConfigError
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        saved = {}
    if saved.get("bucket") != bucket or saved.get("folder") != folder:
        raise ListError(
            f"Your team's opt-out list hasn't been fetched for the team folder under [store], so "
            f"{step}. Start an export (/slack-export) to fetch it, then run again."
        )
