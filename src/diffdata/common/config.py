"""Read config.toml, each person's private settings.

config.toml stays on the user's computer, in ~/.diffdata/ so that tools working in the
repo, such as Codex, can't read it. A config.toml in the current folder still works; it's
gitignored. The data folder always comes through `data_dir()`, so a folder inside a repo
or a synced folder is refused.
"""

import difflib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from diffdata.common.paths import data_dir

CONFIG_NAME = "config.toml"
# A canvas is listed by its ID, or by its link: https://<workspace>.slack.com/docs/<team>/<ID>
CANVAS_ID = re.compile(r"F[A-Z0-9]{6,}")
CANVAS_LINK = re.compile(r"https://[a-z0-9-]+\.slack\.com/docs/T[A-Z0-9]+/(F[A-Z0-9]{6,})/?")
# A channel is listed by its name, its ID, or a link to it or to one of its messages:
# https://<workspace>.slack.com/archives/<ID>, maybe followed by /p<digits>. Names are lowercase,
# IDs uppercase, so an ID is told apart before the name is made lowercase.
# A real ID always has a digit, so a name typed in capitals (GENERAL) stays a name.
CHANNEL_ID = re.compile(r"[CG](?=[A-Z]*[0-9])[A-Z0-9]{6,}")
CHANNEL_LINK = re.compile(
    r"https://([a-z0-9-]+)\.slack\.com/archives/([CG](?=[A-Z]*[0-9])[A-Z0-9]{6,})"
    r"(?:/p\d+)?/?(?:\?.*)?"
)
# CON-5: a direct message ("D..." ID or link) or a group DM (Slack names it "mpdm-...") is never
# collected: the other people in it never agreed. Listing one is refused, and the save hook
# refuses a read of a channel whose name says it's a group DM.
DM_ID = re.compile(r"D(?=[A-Z]*[0-9])[A-Z0-9]{6,}")
DM_LINK = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/D(?=[A-Z]*[0-9])[A-Z0-9]{6,}(?:/p\d+)?/?(?:\?.*)?"
)
GROUP_DM_PREFIX = "mpdm-"
# What setup takes as a channel name: no spaces, dots, slashes or other marks a pasted
# sentence or link has. load_config() doesn't check it yet, so no existing config breaks.
CHANNEL_NAME = re.compile(r"[^\s./#,:;<>@'\"]{1,80}")
# A workspace's address: https://<name>.slack.com
WORKSPACE_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.slack\.com")
# A Google Doc is listed by its ID, or by its link, from the address bar or Drive's "open" form.
DOC_ID = re.compile(r"[A-Za-z0-9_-]{20,}")
DOC_LINK = re.compile(
    r"https://docs\.google\.com/document(?:/u/\d+)?/d/([A-Za-z0-9_-]{20,})(?:[/?#].*)?"
    r"|https://drive\.google\.com/open\?id=([A-Za-z0-9_-]{20,})(?:&.*)?"
)
# A Slack user ID, as the connector shows it: U or W, then 8 or more capitals and digits, at
# least one a digit. The same shape the scrub's people list takes (scrub/people.py).
USER_ID = re.compile(r"[UW](?=[A-Z]*[0-9])[A-Z0-9]{8,}")
OPT_OUT_KEY = "slack_opted_out"
# The team's opt-out list as last fetched from the store (discover/team_optouts.py), kept in
# the data folder. It only grows, and load_config() adds it to slack_opted_out (#132).
TEAM_OPT_OUT_NAME = "team_opted_out.json"
TEAM_OPT_OUT_FORMAT = "diffdata-team-opted-out"
NOTICES_KEY = "slack_notices"
# A notice is named by its message link, from Slack's "Copy link":
# https://<workspace>.slack.com/archives/<channel ID>/p<16 digits>. Only the post itself, not a
# reply in a thread, since a reply isn't what the channel sees pinned.
NOTICE_LINK = re.compile(
    r"https://[a-z0-9-]+(?:\.enterprise)?\.slack\.com/archives/([CG][A-Z0-9]{6,})/p(\d{10})(\d{6})/?"
)
# A channel where everyone agreed directly, so there's no notice to check joins against.
NO_NOTICE = "none"
# Every setting [local] and [store] take. Anything else is a typo, and stops the run.
LOCAL_KEYS = {"data_dir"}
STORE_KEYS = {"backend", "bucket", "folder", "producer", "project", "send_not_approved"}
# A key that looks like a setting name is safe to print. Anything else may be a pasted ID or
# name, so it's never shown.
SETTING_NAME = re.compile(r"(?:top-level )?(?:[a-z_]+\.)*[a-z_]{1,40}")


def shown_key(key: str, known: set[str] | None = None) -> str:
    """The key as typed, if it looks like a setting name and, when `known` is given, is close
    to one of them: a typo worth showing. Anything else might be a pasted ID or name."""
    name = key.rsplit(".", 1)[-1].removeprefix("top-level ")
    if not SETTING_NAME.fullmatch(key):
        return "a key that isn't a setting name"
    if known is not None and not difflib.get_close_matches(name, sorted(known), cutoff=0.7):
        table = key.rsplit(".", 1)[0] if "." in key else "the top level"
        return f"a key under {table} that isn't a setting name"
    return key


# Every setting [sources] takes. Anything else is a typo, and stops the run.
SOURCES_KEYS = {
    "slack_channels",
    "slack_workspace_url",
    "slack_canvases",
    "canvas_refresh_days",
    "google_docs",
    "folders",
    OPT_OUT_KEY,
    NOTICES_KEY,
}


def opt_out_keys(table: dict, where: str) -> list[str]:
    """Where a key that looks like the opt-out list sits, by table: anything but
    [sources] slack_opted_out itself would otherwise be silently ignored."""
    found = []
    for key, value in table.items():
        squashed = re.sub(r"[^a-z]", "", key.lower())
        if "optout" in squashed or "optedout" in squashed:
            found.append(f"{where}{key}")
        # slack_notices is keyed by channel names, and a channel may be named like the list.
        if isinstance(value, dict) and f"{where}{key}" != f"sources.{NOTICES_KEY}":
            found += opt_out_keys(value, f"{where}{key}.")
    return found


# A canvas is read again at least this often, even if nothing says it changed (COL-8).
CANVAS_REFRESH_DAYS = 7


class ConfigError(Exception):
    """config.toml is missing, unreadable, or doesn't have a setting we need."""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # CON-1: listing a channel is the opt-in to collecting it. Names, without the '#'.
    slack_channels: tuple[str, ...]
    # CON-1: channels listed by their ID, or by a link, in slack_channels. IDs.
    slack_channel_ids: tuple[str, ...] = ()
    # Every channel as slack_channels lists it, in its order: a name, or an ID.
    listed_channels: tuple[str, ...] = ()
    # The workspace's address, like https://example.slack.com, for message links.
    slack_workspace_url: str = ""
    # CON-1: listing a canvas is the opt-in to collecting it (COL-2). Canvas IDs.
    slack_canvases: tuple[str, ...] = ()
    # COL-8: read a canvas again after this many days, even if nothing says it changed.
    canvas_refresh_days: int = CANVAS_REFRESH_DAYS
    # CON-1: a doc is fetched only if it's listed here or approved at review, and picked in
    # Google's Picker. IDs.
    google_docs: tuple[str, ...] = ()
    # CON-4, CON-5: Slack user IDs of people who opted out. Their messages are left out of
    # everything the build saves. Real IDs, so they live here only, never in code or output.
    slack_opted_out: tuple[str, ...] = ()
    # CON-5: each channel's pinned notice, as (channel, "<channel ID>/<message ts>"), or
    # (channel, "none") for a channel with no notice. The channel is its name, or its ID for
    # an entry keyed by ID or link. Anyone whose latest join is after the notice is left out
    # of that channel like an opted-out person (#118).
    slack_notices: tuple[tuple[str, str], ...] = ()
    # STO-1, STO-2: where `diffdata push` sends the scrubbed copies. Checked by the push
    # itself, so other commands still run while [store] is empty.
    store_backend: str = ""
    store_bucket: str = ""
    store_folder: str = ""
    store_producer: str = ""
    store_project: str = ""
    # "yes" lets a push send copies not yet approved for release, to their own part of the
    # team folder (store/push.py). Empty or "no" refuses them.
    store_send_not_approved: str = ""
    # Which config.toml these settings came from.
    path: Path | None = None

    def lists_channel(self, channel_id: str, name: str = "") -> bool:
        """Whether slack_channels lists this channel, by its ID or by its name (CON-1)."""
        return channel_id in self.slack_channel_ids or (
            bool(name) and channel_name(name) in self.slack_channels
        )

    def notice(self, channel_id: str, name: str | None = None) -> str | None:
        """This channel's slack_notices value, from an entry keyed by its ID (or a link to it)
        or by its name, as slack_channels can list it either way. None if it has neither. Two
        entries that disagree are refused, since either could be the one meant (#118)."""
        notices = dict(self.slack_notices)
        by_id = notices.get(channel_id)
        by_name = notices.get(channel_name(name)) if name else None
        if by_id is not None and by_name is not None and by_id != by_name:
            raise ConfigError(
                "slack_notices in config.toml has two different entries for one channel, one "
                "by its name and one by its ID or link. Keep one."
            )
        return by_id if by_id is not None else by_name


def channel_name(name: str) -> str:
    """Slack channel names are lowercase; people sometimes type the '#'."""
    return name.strip().lstrip("#").lower()


def channel_id(value: str) -> str | None:
    """A channel's ID, from its ID or a link to it. None if it's neither."""
    value = value.strip()
    if CHANNEL_ID.fullmatch(value):
        return value
    link = CHANNEL_LINK.fullmatch(value)
    return link[2] if link else None


def is_group_dm_name(name: str) -> bool:
    """Whether a channel name is Slack's name for a group DM."""
    return channel_name(name).startswith(GROUP_DM_PREFIX)


def is_direct_message(value: str) -> bool:
    """Whether a slack_channels entry is a DM or a group DM: a D... ID, a link to one, or an
    mpdm-... name (CON-5)."""
    value = value.strip()
    return bool(DM_ID.fullmatch(value) or DM_LINK.fullmatch(value)) or is_group_dm_name(value)


def link_workspace(value: str) -> str | None:
    """The workspace address in a channel link, like https://example.slack.com."""
    link = CHANNEL_LINK.fullmatch(value.strip())
    return f"https://{link[1]}.slack.com" if link else None


def canvas_id(value: str) -> str | None:
    """A canvas's ID, from its ID or its link. None if it's neither."""
    value = value.strip()
    if CANVAS_ID.fullmatch(value):
        return value
    link = CANVAS_LINK.fullmatch(value)
    return link[1] if link else None


def doc_id(value: str) -> str | None:
    """A Google Doc's ID, from its ID or its link. None if it's neither."""
    value = value.strip()
    if DOC_ID.fullmatch(value):
        return value
    link = DOC_LINK.fullmatch(value)
    return (link[1] or link[2]) if link else None


def team_opted_out_path(data: Path) -> Path:
    return data / "slack" / TEAM_OPT_OUT_NAME


def read_team_opted_out(data: Path) -> list[str]:
    """The IDs in the team's fetched opt-out list, or [] if none was fetched yet. A file that's
    there but damaged stops the run: reading it as empty would let opted-out people back in."""
    path = team_opted_out_path(data)
    if not path.exists():
        return []
    damaged = ConfigError(
        f"{TEAM_OPT_OUT_NAME} in the slack folder of your data folder is damaged, so who opted "
        "out can't be known. Delete it, then start an export to fetch the team's list again. "
        "Nothing was collected."
    )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise damaged from None
    if not isinstance(found, dict):
        raise damaged
    ids = found.get("slack_opted_out")
    if (
        found.get("format") != TEAM_OPT_OUT_FORMAT
        or not isinstance(ids, list)
        or not all(isinstance(i, str) and USER_ID.fullmatch(i) for i in ids)
    ):
        raise damaged
    return list(dict.fromkeys(ids))


def user_config_file() -> Path:
    """config.toml's place outside any repo: ~/.diffdata/config.toml."""
    return Path.home() / ".diffdata" / CONFIG_NAME


def find_config() -> Path:
    """Where config.toml is: ~/.diffdata/ first, then the current folder."""
    outside = user_config_file()
    here = Path.cwd() / CONFIG_NAME
    if outside.is_file() and here.is_file() and outside.resolve() != here.resolve():
        raise ConfigError(
            f"There are two {CONFIG_NAME} files: {outside} and {here}. "
            f"Keep the one in {outside.parent} and delete the other."
        )
    if outside.is_file():
        return outside
    if here.is_file():
        return here
    raise ConfigError(
        f"No {CONFIG_NAME} in {outside.parent} or in this folder. "
        f"Copy config.example.toml to {outside} and fill it in."
    )


def load_config(path: str | Path | None = None) -> Config:
    """Read config.toml from `path`, or find it with `find_config()`."""
    path = Path(path) if path is not None else find_config()
    if not path.is_file():
        raise ConfigError(
            f"No {CONFIG_NAME} at {path}. Copy config.example.toml to {CONFIG_NAME} and fill it in."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path} can't be read as TOML: {e}") from e

    configured = raw.get("local", {}).get("data_dir")
    if not configured:
        raise ConfigError(f"{path} has no data_dir under [local].")
    sources = raw.get("sources", {})
    channels = sources.get("slack_channels", [])
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise ConfigError(
            f"slack_channels in {path} must be a list of channel names, IDs or links."
        )
    workspace = sources.get("slack_workspace_url", "")
    if not isinstance(workspace, str) or (workspace and not workspace.startswith("https://")):
        raise ConfigError(f"slack_workspace_url in {path} must start with https://")
    channel_names: list[str] = []
    channel_ids: dict[str, int] = {}  # ID to its entry number
    listed_channels: list[str] = []
    for number, listed in enumerate(channels, start=1):
        if is_direct_message(listed):
            raise ConfigError(
                f"Entry {number} in slack_channels in {path} is a direct message or a group DM. "
                "diffdata never collects those: the other people in them never agreed (CON-5). "
                "Remove it. If it's a channel's name typed in capitals, type it in lowercase. "
                "Nothing was collected."
            )
        found = channel_id(listed)
        if found is not None:
            if found in channel_ids:
                raise ConfigError(
                    f"Entries {channel_ids[found]} and {number} in slack_channels in {path} are "
                    "the same channel. Remove one, or replace it with the channel you meant."
                )
            channel_ids[found] = number
            listed_channels.append(found)
        elif listed.strip().lower().startswith(("http://", "https://")):
            # Messages name the entry's place, not the link, so no ID reaches shared output.
            raise ConfigError(
                f"Entry {number} in slack_channels in {path} is a link, but not to a Slack "
                "channel (https://<workspace>.slack.com/archives/C...)."
            )
        else:
            channel_names.append(channel_name(listed))
            listed_channels.append(channel_name(listed))
    canvases = sources.get("slack_canvases", [])
    if not isinstance(canvases, list) or not all(isinstance(c, str) for c in canvases):
        raise ConfigError(f"slack_canvases in {path} must be a list of canvas IDs or links.")
    # Messages name an entry's place in the list, not its ID, so no ID reaches shared output.
    canvas_ids: list[str] = []
    for number, listed in enumerate(canvases, start=1):
        found = canvas_id(listed)
        if found is None:
            raise ConfigError(
                f"Entry {number} in slack_canvases in {path} isn't a canvas ID (F...) or a "
                "canvas link (https://<workspace>.slack.com/docs/T.../F...)."
            )
        if found in canvas_ids:
            # Often a paste of the wrong line, so the canvas meant here is missing.
            raise ConfigError(
                f"Entries {canvas_ids.index(found) + 1} and {number} in slack_canvases in {path} "
                "are the same canvas. Remove one, or replace it with the canvas you meant."
            )
        canvas_ids.append(found)
    docs = sources.get("google_docs", [])
    if not isinstance(docs, list) or not all(isinstance(d, str) for d in docs):
        raise ConfigError(f"google_docs in {path} must be a list of Google Doc IDs or links.")
    doc_ids: list[str] = []
    for number, listed in enumerate(docs, start=1):
        found = doc_id(listed)
        if found is None:
            raise ConfigError(
                f"Entry {number} in google_docs in {path} isn't a Google Doc ID or a link like "
                "https://docs.google.com/document/d/.../edit."
            )
        if found in doc_ids:
            raise ConfigError(
                f"Entries {doc_ids.index(found) + 1} and {number} in google_docs in {path} are "
                "the same doc. Remove one, or replace it with the doc you meant."
            )
        doc_ids.append(found)
    misplaced = [k for k in opt_out_keys(raw, "") if k != f"sources.{OPT_OUT_KEY}"]
    misplaced = [shown_key(k) for k in misplaced]
    if misplaced:
        # A paste at the end of the file lands in the last table, and a misspelling is just an
        # unknown key. Either would leave no one out, so neither is allowed.
        raise ConfigError(
            f"{path} has {', '.join(misplaced)}. The opt-out list must be slack_opted_out, "
            "under [sources]. Move it there, then run again. Nothing was collected."
        )
    unknown = [
        shown_key(f"sources.{key}", SOURCES_KEYS) for key in sorted(set(sources) - SOURCES_KEYS)
    ]
    for table, known in (("local", LOCAL_KEYS), ("store", STORE_KEYS)):
        section = raw.get(table, {})
        if isinstance(section, dict):
            unknown += [shown_key(f"{table}.{key}", known) for key in sorted(set(section) - known)]
    unknown += sorted(
        shown_key(f"top-level {key}", set())
        for key, value in raw.items()
        if not isinstance(value, dict)
    )
    if unknown:
        # A misspelt setting is otherwise ignored without a word, and for slack_opted_out
        # that would leave no one out (#105).
        raise ConfigError(
            f"{path} has a setting diffdata doesn't know: {', '.join(unknown)}. Check the "
            "spelling against config.example.toml. Nothing was collected."
        )
    opted_out = sources.get(OPT_OUT_KEY, [])
    if not isinstance(opted_out, list) or not all(isinstance(u, str) for u in opted_out):
        raise ConfigError(f"slack_opted_out in {path} must be a list of Slack user IDs.")
    user_ids: list[str] = []
    for number, listed in enumerate(opted_out, start=1):
        found = listed.strip()
        # A typo would silently leave no one out, so it stops the run instead.
        if not USER_ID.fullmatch(found):
            raise ConfigError(
                f"Entry {number} in slack_opted_out in {path} isn't a Slack user ID "
                "(U or W, then 8 or more capital letters and digits). Nothing was collected."
            )
        if found in user_ids:
            raise ConfigError(
                f"Entries {user_ids.index(found) + 1} and {number} in slack_opted_out in {path} "
                "are the same person. Remove one, or replace it with the ID you meant."
            )
        user_ids.append(found)
    notices = read_notices(sources.get(NOTICES_KEY, {}), path)
    data = data_dir(configured)
    # The team's list adds to slack_opted_out, never replaces it (#132, item 12).
    user_ids += [i for i in read_team_opted_out(data) if i not in user_ids]
    refresh_days = sources.get("canvas_refresh_days", CANVAS_REFRESH_DAYS)
    # bool is a kind of int in Python, so true would otherwise pass as 1.
    if isinstance(refresh_days, bool) or not isinstance(refresh_days, int) or refresh_days < 1:
        raise ConfigError(
            f"canvas_refresh_days in {path} must be a whole number of days, 1 or more."
        )

    store = raw.get("store", {})
    if not isinstance(store, dict):
        raise ConfigError(f"[store] in {path} must be a section of settings.")
    for key, value in store.items():
        if not isinstance(value, str):
            # Named, since only our own keys get this far, but its value isn't quoted.
            raise ConfigError(f"{key} under [store] in {path} must be text, in quotes.")

    return Config(
        data_dir=data,
        slack_channels=tuple(channel_names),
        slack_channel_ids=tuple(channel_ids),
        listed_channels=tuple(listed_channels),
        slack_workspace_url=workspace.rstrip("/"),
        slack_canvases=tuple(canvas_ids),
        canvas_refresh_days=refresh_days,
        google_docs=tuple(doc_ids),
        slack_opted_out=tuple(user_ids),
        slack_notices=notices,
        store_backend=store.get("backend", ""),
        store_bucket=store.get("bucket", ""),
        store_folder=store.get("folder", ""),
        store_producer=store.get("producer", ""),
        store_project=store.get("project", ""),
        store_send_not_approved=store.get("send_not_approved", ""),
        path=path,
    )


def read_notices(table, path: Path) -> tuple[tuple[str, str], ...]:
    """slack_notices: each listed channel's notice link, or "none". Checked here for shape
    only. Whether a channel being built has an entry, and whether its link is that channel's,
    is checked at build time, when the channel's ID is known (discover/slack_joins.py). A link
    is never printed: it's a real message's address."""
    if not isinstance(table, dict):
        raise ConfigError(
            f"slack_notices in {path} must be a table of channel names and notice links, like "
            'slack_notices = { "example-channel" = "https://<workspace>.slack.com/archives/'
            'C.../p..." }.'
        )
    # An entry for a channel no longer in slack_channels is allowed: its saved data is still
    # built, so it still needs one. A typo still stops the build, since the channel it was
    # meant for then has no entry (discover/slack_joins.py).
    found: dict[str, str] = {}
    for number, (key, value) in enumerate(table.items(), start=1):
        # Keyed by the channel's name, or by its ID or a link to it, as slack_channels can
        # list it. An ID is never printed: it's a real channel's.
        by_id = channel_id(key)
        if by_id is None and "://" in key:
            raise ConfigError(
                f"A key in slack_notices in {path} is a link, but not to a Slack channel "
                "(https://<workspace>.slack.com/archives/C...). Key it by the channel's name "
                "or ID instead."
            )
        name = by_id or channel_name(key)
        # A key is never printed: it may be an ID, or something pasted, even a token.
        shown = f"entry {number}"
        if name in found:
            raise ConfigError(
                f"slack_notices in {path} has two entries for the same channel. Keep one."
            )
        if not isinstance(value, str):
            raise ConfigError(
                f"The slack_notices {shown} in {path} must be a notice link, in quotes."
            )
        value = value.strip()
        if value.lower() == NO_NOTICE:
            found[name] = NO_NOTICE
            continue
        link = NOTICE_LINK.fullmatch(value)
        if link is None:
            raise ConfigError(
                f"The slack_notices {shown} in {path} isn't a link to a Slack message "
                "(https://<workspace>.slack.com/archives/C.../p...). Use Copy link on the "
                'pinned notice itself, not a reply to it, or write "none" for a channel with '
                "no notice. Nothing was collected."
            )
        found[name] = f"{link[1]}/{link[2]}.{link[3]}"
    return tuple(sorted(found.items()))
