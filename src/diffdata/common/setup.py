"""`diffdata setup` (ENG-1, CON-1, STO-1). Plans: Issue #130, and the team file from #132.

Setup asks the producer three questions in their own terminal (the channels, the workspace
address and their handle), and writes ~/.diffdata/config.toml for them. It takes the team
values (bucket, folder, project), the store key and the Google client file from the team
file the project team sends, makes the data folder, and puts the key and the client file in
place. So nobody edits TOML by hand, or saves a key by hand.

The team file is JSON, written by the project team (later by `diffdata admin add-team`):

  {
    "format": "diffdata-team",
    "version": 1,
    "bucket": "example-bucket",
    "folder": "team-a",
    "project": "example-project",
    "store_key": { ...the team's service account key, as Google gives it... },
    "google_client": { "installed": { "client_id": "...", ... } }
  }

google_client may be left out. Any other key is refused. It holds the team's key, so it's sent
privately, never through the repo, an Issue or a PR, and it's named <team>.diffdata-team.json,
which Claude's tools may not read (.claude/settings.json).

The producer handle is a pseudonym: it labels the producer's copies in the shared bucket,
in place of their name. Setup makes one up from the project name and 4 random characters. It
never reads the computer's user name, the Slack name or the Git name for it.

What setup refuses (the plan's must-refuse list on #130; 15 to 18 as reworded for the team
file, waiting for approval there):
  1   to run unless its input and output are a terminal a person types in
  2   to overwrite a config without a typed yes; it keeps the old one as config.toml.bak, and
      replaces an old .bak only on a second yes
  3   to drop a setting: every key it doesn't ask about stays as it was (slack_opted_out above
      all), checked by reading the new text back
  4   to write a file that load_config() or the push's own checks refuse
  5   to leave a half-written file: it writes a temporary file, checks it, then moves it
      into place in one step
  6   to add a channel the person didn't type or paste
  7   a data folder inside a git repository or a synced folder (data_dir() decides)
  8   a bucket, folder, project or handle that `push` would refuse, with messages that name the
      setting, not the value
  9   to print anything from store-key.json
  10  to change the key file's mode without a typed yes
  11  to delete or move a config
  12  to send anything anywhere: it makes no network call
  13  to take the handle from the computer or from Slack
  14  to change a saved handle by itself
  15  to take team values from anywhere but the team file the producer gives, or the
      config.toml setup wrote before (the values the producer accepted then)
  16  to use team values without showing them first and getting a typed yes
  17  a direct message or a group DM as a channel (CON-5); the save hook also refuses a read
      of a group DM (discover/slack_inbox.py)
  18  to print anything from the store key or the Google client file in a team file, to put
      them anywhere but ~/.diffdata/, readable only by the producer, to replace one that's
      there without a typed yes, or to delete the team file without a typed yes
"""

import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from diffdata.common import config as config_module
from diffdata.common.config import (
    CHANNEL_NAME,
    WORKSPACE_URL,
    ConfigError,
    channel_id,
    channel_name,
    is_direct_message,
    link_workspace,
    load_config,
)
from diffdata.common.paths import UnsafeDataDirError, data_dir, find_git_root, synced_by
from diffdata.store import push

TEAM_KEYS = ("bucket", "folder", "project")
DEFAULT_DATA_DIR = "~/diffdata-data"
BACKUP_NAME = "config.toml.bak"
HANDLE_CHARS = string.ascii_lowercase + string.digits
HANDLE_SUFFIX = 4
PSEUDONYM = (
    "This handle is a pseudonym: it labels your copies in the shared store instead of your name."
)
NOT_A_TERMINAL = (
    "diffdata setup asks you questions, so it only runs in a terminal you type in: open "
    "Terminal or PowerShell and run it there. Nothing was written."
)
NOTHING_WRITTEN = "Nothing was written."
NEXT_STEP = "Next: open Claude Code in the repo folder and run /slack-export."
TEAM_FORMAT = "diffdata-team"
TEAM_VERSION = 1
TEAM_FILE_KEYS = {"format", "version", *TEAM_KEYS, "store_key", "google_client"}
TEAM_FILE_LIMIT = 256_000
CLIENT_NAME = "google-client.json"
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class SetupError(Exception):
    """Setup can't go on. The message says what to do, and quotes no key."""


@dataclass
class Prompter:
    """How setup talks to the person: `ask` shows a question and returns the answer, `say`
    shows a line. Tests replace both."""

    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print

    def yes(self, question: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.ask(f"{question} {hint} ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please type y or n.")


# --- the team values ---------------------------------------------------------------------


def check_team(values: dict[str, str]) -> dict[str, str]:
    """The team values, checked with the push's own rules. A message names the key, never
    the value."""
    if not push.bucket_name(values["bucket"]):
        raise SetupError(
            "bucket isn't a Google Cloud Storage bucket name: 3 to 63 lowercase letters, digits, "
            "- and _, or dotted parts of up to 63 each, with no spaces."
        )
    for key in ("folder", "project"):
        if not push.SEGMENT.fullmatch(values[key]):
            raise SetupError(
                f"{key} must be one plain name: letters, digits, - and _ only, with no / or spaces."
            )
    return values


@dataclass(frozen=True)
class TeamFile:
    """A team file, checked. The key and the client file are kept as the bytes to write."""

    path: Path
    values: dict[str, str]
    store_key: bytes
    google_client: bytes | None


def read_team_file(path: Path) -> TeamFile:
    """Read and check a team file. Messages name what's wrong, never a value, key or secret."""
    path = path.expanduser()
    raw = push.read_plain(path.parent, path.name) if path.name else None
    if raw is None or len(raw) > TEAM_FILE_LIMIT:
        raise SetupError(
            "That isn't a team file: give the path of the plain file the project team sent, "
            "not a folder or a link."
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    version = data.get("version") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != TEAM_FORMAT
        or isinstance(version, bool)
        or version != TEAM_VERSION
    ):
        raise SetupError(
            f"That isn't a diffdata team file (format {TEAM_FORMAT}, version {TEAM_VERSION}). "
            "Ask the project team to send it again."
        )
    unknown = sorted(set(data) - TEAM_FILE_KEYS)
    if unknown:
        shown = ", ".join(config_module.shown_key(k, TEAM_FILE_KEYS) for k in unknown)
        raise SetupError(f"The team file has a setting setup doesn't know: {shown}.")
    missing = [k for k in (*TEAM_KEYS, "store_key") if k not in data]
    if missing:
        raise SetupError(f"The team file has no {', '.join(missing)}.")
    if not all(isinstance(data[k], str) for k in TEAM_KEYS):
        raise SetupError("bucket, folder and project in the team file must be text.")
    values = check_team({k: data[k].strip() for k in TEAM_KEYS})
    try:
        push.check_key_info(data["store_key"])
    except push.PushError:
        raise SetupError(
            "The store key in the team file isn't a Google service account key that signs in "
            "only with Google. Ask the project team for a new team file."
        ) from None
    client = data.get("google_client")
    if client is not None:
        installed = client.get("installed") if isinstance(client, dict) else None
        if not isinstance(installed, dict) or not isinstance(installed.get("client_id"), str):
            raise SetupError(
                "The Google client file in the team file isn't a Desktop app client. Ask the "
                "project team for a new team file."
            )
    as_bytes = lambda part: (json.dumps(part, indent=2) + "\n").encode("utf-8")  # noqa: E731
    return TeamFile(
        path=path,
        values=values,
        store_key=as_bytes(data["store_key"]),
        google_client=None if client is None else as_bytes(client),
    )


# --- the producer's answers --------------------------------------------------------------


def parse_channels(text: str) -> tuple[list[str], str | None]:
    """The channels as config.toml stores them (a lowercase name, or the ID from a link or
    an ID), and the workspace address the links name, if any."""
    entries: list[str] = []
    workspaces: set[str] = set()
    for number, part in enumerate((p for p in re.split(r"[,\s]+", text) if p), start=1):
        if is_direct_message(part):
            raise SetupError(
                f"Channel {number} is a direct message or a group DM. diffdata never collects "
                "those: the other people in them never agreed (CON-5). List channels only."
            )
        found = channel_id(part)
        if found is not None:
            workspace = link_workspace(part)
            if workspace:
                workspaces.add(workspace)
            entry = found
        elif part.lower().startswith(("http://", "https://")):
            raise SetupError(
                f"Channel {number} is a link, but not to a Slack channel. Copy the channel's link "
                "in Slack (it has /archives/ in it), or type the channel's name."
            )
        else:
            entry = channel_name(part)
            if not CHANNEL_NAME.fullmatch(entry):
                raise SetupError(
                    f"Channel {number} isn't a channel name: names have no spaces or dots, and "
                    "are at most 80 characters."
                )
        if entry not in entries:
            entries.append(entry)
    if not entries:
        raise SetupError("Type at least one channel. Listing a channel is your opt-in (CON-1).")
    if len(workspaces) > 1:
        raise SetupError("The channel links are from more than one workspace. Use one workspace.")
    return entries, next(iter(workspaces), None)


def parse_workspace(text: str) -> str:
    """The workspace address as https://<name>.slack.com, from that, <name>.slack.com or
    <name>."""
    value = text.strip().lower().rstrip("/")
    if value and "://" not in value:
        value = f"https://{value}"
    if value and "." not in value.removeprefix("https://"):
        value = f"{value}.slack.com"
    if not WORKSPACE_URL.fullmatch(value):
        raise SetupError(
            "That isn't a Slack workspace address. It looks like https://example.slack.com: "
            "copy it from your browser's address bar when Slack is open."
        )
    return value


def new_handle(project: str, choose: Callable[[str], str] = secrets.choice) -> str:
    """A made-up handle: the project name and 4 random characters. Nothing about the person."""
    suffix = "".join(choose(HANDLE_CHARS) for _ in range(HANDLE_SUFFIX))
    return f"{project[: 63 - HANDLE_SUFFIX]}-{suffix}"


def check_handle(text: str) -> str:
    handle = text.strip()
    if not push.SEGMENT.fullmatch(handle):
        raise SetupError(
            "A handle is letters, digits, - and _ only, up to 64 characters, with no spaces."
        )
    return handle


# --- writing config.toml -----------------------------------------------------------------


def toml_key(key: str) -> str:
    return key if BARE_KEY.fullmatch(key) else toml_string(key)


def toml_string(value: str) -> str:
    # JSON's escapes are all TOML escapes too. TOML also bars DEL in a string.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")


def toml_value(value, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(toml_string(item) for item in value) + "]"
    # Setup never drops a setting, so one it can't write again stops it instead.
    raise SetupError(
        f"Setup can't keep {where} as it is in your config.toml. Change it there by hand, or "
        f"move the file away and run setup again. {NOTHING_WRITTEN}"
    )


TABLE_ORDER = ("sources", "local", "store")
HEADER = """\
# diffdata settings, written by `diffdata setup`. Run it again to change them.
# Listing a channel under slack_channels is your opt-in to collecting it (CON-1).
# producer is a pseudonym: it labels your copies in the shared store instead of your name.
# Never commit this file, and never open it in an AI tool.
"""


def render(raw: dict) -> str:
    """config.toml's text for `raw`: plain values and one level of tables, as diffdata uses.
    Reading the text back gives `raw` exactly, or setup stops."""
    lines = [HEADER]
    tables = [key for key in TABLE_ORDER if isinstance(raw.get(key), dict)]
    tables += [key for key, value in raw.items() if isinstance(value, dict) and key not in tables]
    for key, value in raw.items():
        if not isinstance(value, dict):
            lines.append(f"{toml_key(key)} = {toml_value(value, key)}")
    for table in tables:
        lines.append(f"\n[{toml_key(table)}]")
        for key, value in raw[table].items():
            lines.append(f"{toml_key(key)} = {toml_value(value, f'{key} under [{table}]')}")
    text = "\n".join(lines) + "\n"
    if tomllib.loads(text) != raw:
        raise SetupError(f"Setup couldn't write your settings exactly. {NOTHING_WRITTEN}")
    return text


def read_raw(path: Path) -> dict:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise SetupError(
            f"{path} can't be read as TOML, so setup can't keep its settings. Fix it by hand, or "
            f"move it away, then run setup again. {NOTHING_WRITTEN}"
        ) from None
    for table in TABLE_ORDER:
        if table in raw and not isinstance(raw[table], dict):
            raise SetupError(
                f"[{table}] in {path} isn't a section of settings. Fix it by hand, or move the "
                f"file away, then run setup again. {NOTHING_WRITTEN}"
            )
    return raw


def write_config(settings: Path, raw: dict, prompter: Prompter) -> bool:
    """Write `raw` to `settings`, keeping the old file as config.toml.bak. False if the person
    chose not to replace an old .bak, so nothing was written."""
    text = render(raw)
    folder = settings.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if settings.is_symlink():
        raise SetupError(
            f"{settings} is a link. Setup writes only a plain config.toml: replace the link "
            f"with the file itself, then run setup again. {NOTHING_WRITTEN}"
        )
    if settings.exists():
        backup = folder / BACKUP_NAME
        if backup.is_symlink() or (backup.exists() and not backup.is_file()):
            # Copying onto a link would write over the file it points to.
            raise SetupError(
                f"{backup} is a link or a folder, not a plain file. Move it away, then run "
                f"setup again. {NOTHING_WRITTEN}"
            )
        if backup.exists() and not prompter.yes(
            f"There's an older {BACKUP_NAME}. Replace it with a copy of the current config.toml?",
            default=False,
        ):
            prompter.say(f"{NOTHING_WRITTEN} Move {backup} away to keep it, then run setup again.")
            return False
        # A private copy first, then one move: the old backup is never gone before the new
        # one is in place, and no one else can read it on the way.
        old, copy_name = tempfile.mkstemp(dir=folder, prefix=".config-bak-", suffix=".toml")
        copy = Path(copy_name)
        try:
            with os.fdopen(old, "wb") as file:
                file.write(settings.read_bytes())
            restrict(copy)
            os.replace(copy, backup)
        finally:
            copy.unlink(missing_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=folder, prefix=".config-", suffix=".toml")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        restrict(temp)
        try:
            push.settings(load_config(temp))
        except (ConfigError, UnsafeDataDirError, push.PushError) as e:
            raise SetupError(f"The new settings don't pass diffdata's checks: {e}") from None
        os.replace(temp, settings)
    finally:
        temp.unlink(missing_ok=True)
    return True


def restrict(path: Path) -> None:
    """Readable and writable by this user only, on macOS and Linux."""
    if os.name != "nt":
        os.chmod(path, 0o600)


# --- the folder and file pickers ---------------------------------------------------------


def system_picker(kind: str) -> Path | None:
    """The system's own folder or file picker, or None if there isn't one or it was
    cancelled. `kind` is "folder" or "file"."""
    if sys.platform == "darwin":
        what = "folder" if kind == "folder" else "file"
        script = f'POSIX path of (choose {what} with prompt "Choose the {kind} for diffdata")'
        command = ["osascript", "-e", script]
    elif sys.platform == "win32":
        dialog = (
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
            if kind == "folder"
            else "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            "if ($d.ShowDialog() -eq 'OK') { $d.FileName }"
        )
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Add-Type -AssemblyName System.Windows.Forms; {dialog}",
        ]
    else:
        return None
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    chosen = done.stdout.strip()
    return Path(chosen) if done.returncode == 0 and chosen else None


# --- the steps ---------------------------------------------------------------------------


def ask_until(prompter: Prompter, question: str, parse: Callable[[str], object]):
    while True:
        try:
            return parse(prompter.ask(question))
        except SetupError as e:
            prompter.say(str(e))


def ask_team_file(prompter: Prompter, picker: Callable[[str], Path | None]) -> TeamFile:
    prompter.say("Give the team file the project team sent you (it ends in .diffdata-team.json).")
    while True:
        answer = prompter.ask("Team file's path, or C to choose it: ").strip()
        chosen = picker("file") if answer.lower() == "c" else (Path(answer) if answer else None)
        if chosen is None:
            continue
        try:
            return read_team_file(chosen)
        except SetupError as e:
            prompter.say(str(e))


def team_step(
    prompter: Prompter,
    team: TeamFile | None,
    store: dict,
    picker: Callable[[str], Path | None],
) -> tuple[dict[str, str], TeamFile | None]:
    """The team values, and the team file they came from (None for the old config.toml)."""
    values: dict[str, str] | None = None
    if team is None:
        try:
            values = check_team({key: str(store[key]) for key in TEAM_KEYS})
        except (KeyError, SetupError):
            team = ask_team_file(prompter, picker)
    while True:
        if team is not None:
            values = team.values
            parts = "a store key" + (" and a Google client file" if team.google_client else "")
            prompter.say(f"The team file {team.path} holds {parts}, and these team values:")
        else:
            prompter.say("Your config.toml has these team values:")
        assert values is not None
        prompter.say(
            f"  bucket {values['bucket']}, folder {values['folder']}, project {values['project']}"
        )
        if prompter.yes("Use these?", default=False):
            return values, team
        team = ask_team_file(prompter, picker)


def folder_step(prompter: Prompter, picker: Callable[[str], Path | None], current: str) -> str:
    """The data folder: the current or default one if data_dir() takes it, or one the person
    chooses. Returned as it will be written."""
    proposed: str | None = current
    while True:
        if proposed is not None:
            try:
                shown = data_dir(proposed)
                if shown.exists() and not shown.is_dir():
                    raise SetupError(f"{shown} is a file, not a folder.")
            except (UnsafeDataDirError, SetupError) as e:
                prompter.say(f"{e} Choose another folder.")
            else:
                answer = prompter.ask(
                    f"Data folder: {shown}. Press Enter to use it, or type C to choose another: "
                ).strip()
                if not answer:
                    return proposed
                if answer.lower() != "c":
                    prompter.say("Press Enter, or type C.")
                    continue
        chosen = picker("folder")
        if chosen is None:
            typed = prompter.ask("Type the folder's full path: ").strip()
            chosen = Path(typed) if typed else None
        # Absolute, so config.toml names the same folder from wherever a command runs.
        proposed = str(chosen.expanduser().resolve()) if chosen else None


def config_step(
    prompter: Prompter,
    team: TeamFile | None,
    raw: dict,
    picker: Callable[[str], Path | None],
    choose: Callable[[str], str],
) -> tuple[dict, TeamFile | None]:
    """Ask, and return the new settings (`raw` with only setup's keys changed) and the team
    file used, if any."""
    sources = raw.get("sources", {}) if isinstance(raw.get("sources"), dict) else {}
    local = raw.get("local", {}) if isinstance(raw.get("local"), dict) else {}
    store = raw.get("store", {}) if isinstance(raw.get("store"), dict) else {}

    values, team = team_step(prompter, team, store, picker)
    folder = folder_step(prompter, picker, str(local.get("data_dir") or DEFAULT_DATA_DIR))

    old_channels = sources.get("slack_channels")
    keep = ", ".join(old_channels) if isinstance(old_channels, list) and old_channels else ""
    prompter.say(
        "Which Slack channels should diffdata collect? Listing a channel is your opt-in. "
        "Type each name, or paste the channel's link, separated by commas."
    )

    def channels_or_keep(text: str):
        return parse_channels(text if text.strip() or not keep else keep)

    question = (
        f"Channels [Enter keeps: {keep}; what you type replaces this list]: "
        if keep
        else "Channels: "
    )
    channels, link_space = ask_until(prompter, question, channels_or_keep)

    old_space = sources.get("slack_workspace_url")
    space_default = link_space or (old_space if isinstance(old_space, str) else "")
    while True:
        question = (
            f"Workspace address [Enter for {space_default}]: "
            if space_default
            else "Workspace address (like https://example.slack.com): "
        )
        space = ask_until(
            prompter, question, lambda t: parse_workspace(t if t.strip() else space_default)
        )
        if link_space and space != link_space:
            prompter.say("That isn't the workspace your channel links are from.")
            continue
        break

    prompter.say(PSEUDONYM)
    try:
        handle_default = check_handle(str(store.get("producer", "")))
    except SetupError:
        handle_default = new_handle(values["project"], choose)
    prompter.say("Use it, or type your own. Never your real name.")
    handle = ask_until(
        prompter,
        f"Producer handle [Enter for {handle_default}]: ",
        lambda t: check_handle(t if t.strip() else handle_default),
    )

    new = deepcopy(raw)
    new_sources = new.setdefault("sources", {})
    new_sources["slack_channels"] = channels
    new_sources["slack_workspace_url"] = space
    new.setdefault("local", {})["data_dir"] = folder
    new_store = new.setdefault("store", {})
    new_store["backend"] = "gcs"
    new_store.update(values)
    new_store["producer"] = handle
    new_store.setdefault("send_not_approved", "no")
    return new, team


def key_step(prompter: Prompter, folder: Path) -> None:
    """Check the store key that's in place. Nothing from it is shown."""
    key = folder / push.KEY_NAME
    if not key.exists() and not key.is_symlink():
        prompter.say(
            f"There's no store key at {key} yet. It comes in the team file: run "
            "`uv run diffdata setup <team file>`. `diffdata push` won't run until it's there."
        )
        return
    try:
        push.read_key(key)
    except push.PushError as e:
        prompter.say(str(e))
        return
    except Exception as e:
        # Its message could quote the key, so only its type is shown.
        prompter.say(f"Couldn't check the store key: an unexpected {type(e).__name__}.")
        return
    prompter.say("Store key found.")
    mode = key.lstat().st_mode
    if os.name != "nt" and mode & 0o077:
        if prompter.yes(
            f"Others on this computer can read {push.KEY_NAME}. Make it readable only by you "
            "(chmod 600)?",
            default=False,
        ):
            os.chmod(key, 0o600)


def install_file(prompter: Prompter, target: Path, data: bytes, label: str) -> None:
    """Put one part of the team file in place, readable only by the producer. A file that's
    there already is replaced only on a typed yes, and never written through a link."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        prompter.say(f"{target} is a link or a folder, so the {label} wasn't put there.")
        return
    if target.exists():
        if target.read_bytes() == data:
            prompter.say(f"The {label} at {target} is the same as the team file's.")
            return
        if not prompter.yes(
            f"There's a different {label} at {target}. Replace it with the team file's?",
            default=False,
        ):
            prompter.say(f"Kept the {label} that was there.")
            return
        handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-")
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            restrict(temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    else:
        # Made only if it isn't there, so one saved meanwhile is never written over. One
        # plain file, which push accepts; a write cut short removes it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            out = os.open(target, flags, 0o600)
        except FileExistsError:
            prompter.say(f"A {label} appeared at {target} meanwhile. It was kept.")
            return
        try:
            with os.fdopen(out, "wb") as file:
                file.write(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        restrict(target)
    prompter.say(f"Saved the {label} at {target}, readable only by you.")


def install_team_file(prompter: Prompter, folder: Path, team: TeamFile) -> None:
    install_file(prompter, folder / push.KEY_NAME, team.store_key, "store key")
    if team.google_client is not None:
        install_file(prompter, folder / CLIENT_NAME, team.google_client, "Google client file")
    if prompter.yes(f"Delete the team file at {team.path}?", default=False):
        team.path.unlink()
        prompter.say("Deleted the team file.")


def check_existing(prompter: Prompter, settings: Path) -> None:
    try:
        push.settings(load_config(settings))
    except (ConfigError, UnsafeDataDirError, push.PushError) as e:
        prompter.say(f"Your config.toml doesn't pass every check yet: {e}")
    else:
        prompter.say("Your config.toml passes diffdata's checks.")


def setup(
    team_file: Path | None = None,
    prompter: Prompter | None = None,
    picker: Callable[[str], Path | None] = system_picker,
    choose: Callable[[str], str] = secrets.choice,
) -> int:
    """The whole of `diffdata setup`, after the terminal check. Returns the exit code."""
    prompter = prompter or Prompter()
    # Through the module, so the tests' guard against the real config covers setup too.
    settings = config_module.user_config_file()
    folder = settings.parent
    here = Path.cwd() / "config.toml"
    if here.is_file() and (not settings.is_file() or here.resolve() != settings.resolve()):
        raise SetupError(
            f"There's a config.toml in this folder ({here}). diffdata keeps its settings in "
            f"{folder}, and stops when it finds two. Move that file away or delete it yourself, "
            f"or run setup from another folder. {NOTHING_WRITTEN}"
        )
    for candidate in (folder.absolute(), folder.resolve()):
        if find_git_root(candidate) is not None or synced_by(candidate) is not None:
            raise SetupError(
                f"{folder} is inside a git repository or a synced folder, where config.toml "
                f"and the store key could be shared by mistake. {NOTHING_WRITTEN}"
            )
    team = read_team_file(team_file) if team_file is not None else None
    raw: dict = {}
    if settings.exists():
        raw = read_raw(settings)
        prompter.say(f"You have a config.toml already, at {settings}.")
        if not prompter.yes("Change it?", default=False):
            if team is not None:
                prompter.say("So the team file wasn't used.")
            check_existing(prompter, settings)
            key_step(prompter, folder)
            return 0
    new, team = config_step(prompter, team, raw, picker, choose)

    store = new["store"]
    prompter.say("")
    prompter.say("You chose:")
    prompter.say(f"  Channels:         {', '.join(new['sources']['slack_channels'])}")
    prompter.say(f"  Workspace:        {new['sources']['slack_workspace_url']}")
    prompter.say(f"  Data folder:      {data_dir(new['local']['data_dir'])}")
    prompter.say(
        f"  Team values:      bucket {store['bucket']}, folder {store['folder']}, "
        f"project {store['project']}"
    )
    prompter.say(f"  Producer handle:  {store['producer']} (a pseudonym)")
    if not prompter.yes(f"Write this to {settings}?", default=False):
        prompter.say(NOTHING_WRITTEN)
        return 0
    try:
        if not write_config(settings, new, prompter):
            return 1
    except OSError as e:
        raise SetupError(f"Couldn't write {settings}: {e}. {NOTHING_WRITTEN}") from None
    prompter.say(f"Wrote {settings}.")
    try:
        data = data_dir(new["local"]["data_dir"])
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        if team is not None:
            install_team_file(prompter, folder, team)
        key_step(prompter, folder)
    except OSError as e:
        raise SetupError(
            f"{settings} was written, but then something failed: {e}. Run setup again to finish."
        ) from None
    prompter.say("")
    prompter.say(f"Settings:     {folder}")
    prompter.say(f"Data folder:  {data}")
    prompter.say(NEXT_STEP)
    return 0
