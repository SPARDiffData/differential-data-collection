"""Read config.toml, each person's private settings.

config.toml stays on the user's computer, in ~/.diffdata/ so that tools working in the
repo, such as Codex, can't read it. A config.toml in the current folder still works; it's
gitignored. The data folder always comes through `data_dir()`, so a folder inside a repo
or a synced folder is refused.
"""

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from diffdata.common.paths import data_dir

CONFIG_NAME = "config.toml"
# A canvas is listed by its ID, or by its link: https://<workspace>.slack.com/docs/<team>/<ID>
CANVAS_ID = re.compile(r"F[A-Z0-9]{6,}")
CANVAS_LINK = re.compile(r"https://[a-z0-9-]+\.slack\.com/docs/T[A-Z0-9]+/(F[A-Z0-9]{6,})/?")
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
}


def opt_out_keys(table: dict, where: str) -> list[str]:
    """Where a key that looks like the opt-out list sits, by table: anything but
    [sources] slack_opted_out itself would otherwise be silently ignored."""
    found = []
    for key, value in table.items():
        squashed = re.sub(r"[^a-z]", "", key.lower())
        if "optout" in squashed or "optedout" in squashed:
            found.append(f"{where}{key}")
        if isinstance(value, dict):
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


def channel_name(name: str) -> str:
    """Slack channel names are lowercase; people sometimes type the '#'."""
    return name.strip().lstrip("#").lower()


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
        raise ConfigError(f"slack_channels in {path} must be a list of channel names.")
    workspace = sources.get("slack_workspace_url", "")
    if not isinstance(workspace, str) or (workspace and not workspace.startswith("https://")):
        raise ConfigError(f"slack_workspace_url in {path} must start with https://")
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
        data_dir=data_dir(configured),
        slack_channels=tuple(channel_name(c) for c in channels),
        slack_workspace_url=workspace.rstrip("/"),
        slack_canvases=tuple(canvas_ids),
        canvas_refresh_days=refresh_days,
        google_docs=tuple(doc_ids),
        slack_opted_out=tuple(user_ids),
        store_backend=store.get("backend", ""),
        store_bucket=store.get("bucket", ""),
        store_folder=store.get("folder", ""),
        store_producer=store.get("producer", ""),
        store_project=store.get("project", ""),
        store_send_not_approved=store.get("send_not_approved", ""),
        path=path,
    )
