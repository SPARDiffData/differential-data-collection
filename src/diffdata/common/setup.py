"""`diffdata setup` (ENG-1, CON-1, STO-1). Plans: Issue #130, and the team file from #132.

Setup runs in the producer's own terminal and writes ~/.diffdata/config.toml for them. On a
first run it asks only for the channels (and the workspace address, if no channel link gives
it) and each channel's notice link (revision 6), chooses the data folder and a made-up
handle, and shows everything on one screen: y writes it, n stops, and c asks each question
in turn (revision 5 of #130). A re-run asks each question in turn, as before. It takes the team
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

What setup refuses (the plan's must-refuse list, approved on #130 and #132; 15 to 18 as
reworded for the team file, and 16 as Lachlan changed it, approved on 2026-11-25):
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
      existing config.toml
  16  to use team values without showing them first and getting a typed yes. On a first
      run, the yes on the summary screen counts, since the screen shows the team values and
      where they come from. Every question takes only the answers it lists (y or n, also
      typed as yes or no; y, n or c on the summary screen): Enter alone, or any other
      answer, asks again
  17  a direct message or a group DM as a channel (CON-5); the save hook also refuses a read
      of a group DM (discover/slack_inbox.py)
  18  to print anything from the store key or the Google client file in a team file, to put
      them anywhere but ~/.diffdata/, readable only by the producer, or to replace one that's
      there without a typed yes. The team file is deleted by itself (#159, approved on
      2026-11-27), but never before its store key, and its Google client file if it has one,
      are saved and read back the same from ~/.diffdata/, and never when what's at the path
      setup was given isn't one plain file (a link, a folder, a file with a second hard link)
      or no longer holds that key. If deleting fails, setup says so and goes on
  19  to write a value the producer hasn't seen: every value setup chose by itself (data
      folder, workspace, team values, handle) is on the summary screen before the yes, with
      the pseudonym line (revision 5, approved on 2026-11-25)

Each channel's notice link (revision 6, approved on 2026-11-25), after the channels question:
  20  to write "none" by itself: Enter leaves the entry out; only a typed none writes it
  21  a link that isn't a Slack message link, or is for another channel or workspace: it's
      asked again, and the link isn't printed
  22  to drop or change an existing entry without a typed answer: Enter keeps it, and every
      entry for a channel no longer listed stays as it was (#119 keeps them for saved data)
  23  to read Slack or anything online to check a link: the check is offline, from the
      link's own parts
  24  to write a notice entry the producer hasn't seen: each channel's notice line is on the
      summary screen before the yes

send_not_approved (#150, item 5, approved on 2026-11-26), asked on every run that writes.
25 to 27 here are #150's must-refuse 1 to 3:
  25  to write "yes" without a typed y: Enter alone, or any other answer, asks again; the
      answer is on the summary screen before the write
  26  to keep or change an existing value without a typed answer: a re-run shows it and still
      asks for y or n; nothing else changes with it
  27  to write anything but "yes" or "no"
"""

import json
import os
import re
import secrets
import shlex
import string
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from diffdata.common import config as config_module
from diffdata.common.config import (
    CHANNEL_ID,
    CHANNEL_NAME,
    DM_ID,
    NO_NOTICE,
    NOTICE_LINK,
    NOTICES_KEY,
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
NEXT_STEP = "Setup is done. After day 7, when the project team tells you, run:"
# The repo folder: src/diffdata/common/setup.py -> the repo.
REPO = Path(__file__).resolve().parents[3]
CHANNELS_HELP = (
    "Which Slack channels should diffdata collect? Listing a channel is your opt-in. For each "
    "channel, paste the link of the notice you pinned in it (the notice's menu, Copy link). "
    "If everyone in a channel agreed directly and there's no notice, paste the channel's link "
    "or type its name instead. Separate them with commas."
)
NOTICE_HELP = "Notice link for {channel}: paste the pinned notice's link (its menu, Copy link),"
NOTICE_QUESTION = "type none if everyone in it agreed directly, or press Enter to {enter}: "
NOT_ADDED = "not added yet"
NOT_APPROVED_HELP = (
    "Copies not approved for release: every scrubbed copy is marked not approved for now, "
    "because names of people who aren't in the people list aren't checked yet. With y, push "
    "sends them to the not-approved part of your team's folder, which is never given to "
    "consumers. With n, push sends none of them, so it sends nothing for now."
)
NOT_APPROVED_QUESTION = "Send copies not approved for release?"
NONE_SHOWN = "none (everyone agreed directly)"
TEAM_FORMAT = "diffdata-team"
TEAM_VERSION = 1
TEAM_FILE_KEYS = {"format", "version", *TEAM_KEYS, "store_key", "google_client"}
TEAM_FILE_LIMIT = 256_000
CLIENT_NAME = "google-client.json"
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def printable(path: Path) -> str:
    """A path to show: control characters (which a terminal could act on, for example to
    clear what setup showed above) become "?"."""
    return "".join("?" if unicodedata.category(c).startswith("C") else c for c in str(path))


class SetupError(Exception):
    """Setup can't go on. The message says what to do, and quotes no key."""


@dataclass
class Prompter:
    """How setup talks to the person: `ask` shows a question and returns the answer, `say`
    shows a line. Tests replace both."""

    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print

    def yes(self, question: str) -> bool:
        """A safety question: only y, yes, n or no is an answer. Enter alone, or anything
        else, asks again, so nothing is changed, or kept, by a key pressed by mistake."""
        while True:
            answer = self.ask(f"{question} [y/n] ").strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please type y or n.")

    def letter(self, question: str, letters: str) -> str:
        """Like `yes`, for a question with more answers: only one of `letters` is an
        answer. Enter alone, or anything else, asks again."""
        shown = "/".join(letters)
        while True:
            answer = self.ask(f"{question} [{shown}] ").strip().lower()
            if len(answer) == 1 and answer in letters:
                return answer
            self.say(f"Please type {', '.join(letters[:-1])} or {letters[-1]}.")


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
            hint = (
                " If it's a channel's name typed in capitals, type it in lowercase."
                if DM_ID.fullmatch(part.strip())
                else ""
            )
            raise SetupError(
                f"Channel {number} is a direct message or a group DM. diffdata never collects "
                f"those: the other people in them never agreed (CON-5). List channels only.{hint}"
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


def pasted_notices(text: str) -> dict[str, str]:
    """The notice links among the channels typed (#156): a link that is exactly a top-level
    Slack message link (NOTICE_LINK) is that channel's notice, keyed by the channel's ID. A
    reply in a thread, or any other link, gives the channel only (must-refuse 2). Two
    different notice links for one channel are refused (must-refuse 5). Call it after
    parse_channels, which refuses direct messages and mixed workspaces."""
    found: dict[str, str] = {}
    for part in (p for p in re.split(r"[,\s]+", text) if p):
        link = NOTICE_LINK.fullmatch(part)
        entry = channel_id(part)
        if link is None or entry is None:
            continue
        if entry in found and not same_notice(found[entry], part):
            raise SetupError(
                f"You pasted two different notice links for the channel {entry}. Paste only "
                "the notice pinned in it."
            )
        found.setdefault(entry, part)
    return found


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
    if isinstance(value, dict) and all(isinstance(item, str) for item in value.values()):
        # An inline table of strings, like slack_notices (#118).
        return (
            "{ " + ", ".join(f"{toml_key(k)} = {toml_string(v)}" for k, v in value.items()) + " }"
        )
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
            f"There's an older {BACKUP_NAME}. Replace it with a copy of the current config.toml?"
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
            prompter.say(
                f"The team file {printable(team.path)} holds {parts}, and these team values:"
            )
        else:
            prompter.say("Your config.toml has these team values:")
        assert values is not None
        prompter.say(
            f"  bucket {values['bucket']}, folder {values['folder']}, project {values['project']}"
        )
        if prompter.yes("Use these?"):
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
                    f"Data folder: {shown}. Press Enter to use it, type C to choose another, "
                    "or type another folder's full path: "
                ).strip()
                if not answer:
                    return proposed
                if answer.lower() != "c":
                    # A typed path gets the same checks as a picked one, then is shown again.
                    proposed = str(Path(answer).expanduser().resolve())
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
    written: bool = True,
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
    old_space = sources.get("slack_workspace_url")
    channels, space, pasted = channels_and_workspace(
        prompter, keep, old_space if isinstance(old_space, str) else "", always_ask=True
    )
    notices = notices_step(prompter, channels, space, notice_table(sources), pasted)

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
    send = not_approved_step(prompter, store, written)
    return with_answers(raw, channels, space, folder, values, handle, notices, send), team


def notice_table(sources: dict) -> dict:
    table = sources.get(NOTICES_KEY, {})
    if not isinstance(table, dict):
        raise SetupError(
            f"slack_notices in your config.toml isn't a table of channels and notice links. Fix "
            f"it by hand, or move the file away, then run setup again. {NOTHING_WRITTEN}"
        )
    return table


def ask_channels(prompter: Prompter, keep: str) -> tuple[list[str], str | None, dict[str, str]]:
    """The channels, the workspace their links are from (None if none was a link), and the
    notice links pasted for them (#156). `keep` is the current list, kept on Enter; "" if
    there is none."""
    prompter.say(CHANNELS_HELP)

    def channels_or_keep(text: str):
        chosen = text if text.strip() or not keep else keep
        channels, space = parse_channels(chosen)
        # Only links typed now are notices: Enter keeps the saved list, and a message link
        # saved there names a channel, never a notice the producer gave here.
        return channels, space, pasted_notices(text) if text.strip() else {}

    question = (
        f"Channels [Enter keeps: {keep}; what you type replaces this list]: "
        if keep
        else "Channels: "
    )
    typed: list[str] = []

    def channels_or_keep_noting(text: str):
        found = channels_or_keep(text)
        typed[:] = [p for p in re.split(r"[,\s]+", text) if CHANNEL_ID.fullmatch(p)]
        return found

    channels, link_space, pasted = ask_until(prompter, question, channels_or_keep_noting)
    for entry in typed:
        # Typed, not from a link: it could be a name in capitals that happens to hold a digit.
        prompter.say(
            f"{entry} is stored as a channel ID. If it's a channel's name, run setup again and "
            "type it in lowercase."
        )
    return channels, link_space, pasted


def channels_and_workspace(
    prompter: Prompter, keep: str, old_space: str, always_ask: bool
) -> tuple[list[str], str, dict[str, str]]:
    """The channels, the workspace and the notice links pasted for them. A link's workspace
    gets the same check as a typed address; if it fails, the channels are asked again, so no
    question can trap the person. With always_ask False, a link's workspace is used without a
    question."""
    while True:
        channels, link_space, pasted = ask_channels(prompter, keep)
        link: str | None = None
        if link_space:
            try:
                link = parse_workspace(link_space)
            except SetupError as e:
                prompter.say(f"{e} Give the channels again.")
                continue
        if link and not always_ask:
            return channels, link, pasted
        return channels, ask_workspace(prompter, link, old_space), pasted


def ask_workspace(prompter: Prompter, link_space: str | None, old_space: str) -> str:
    """The workspace address: the links' one, or the saved one, on Enter. A typed one must
    match the links'."""
    space_default = link_space or old_space
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
        return space


def shown_channel(entry: str) -> str:
    return f"{entry} (a channel ID)" if CHANNEL_ID.fullmatch(entry) else entry


def notice_entry(table: dict, entry: str) -> tuple[str, object] | None:
    """This channel's slack_notices key and value, keyed by its name, its ID or a link to it,
    as config.py reads them. None if it has none."""
    for key, value in table.items():
        if (channel_id(key) or channel_name(key)) == entry:
            return key, value
    return None


def notice_time(ts: str, seconds: bool = False) -> str:
    """A notice's time from its link's ts, in this computer's time zone, as Slack shows it.
    With seconds, to tell apart two notices posted in the same minute."""
    when = datetime.fromtimestamp(int(ts), tz=UTC).astimezone()
    # An offset, not %Z: Windows gives long zone names, such as "W. Europe Standard Time".
    return when.strftime(f"%Y-%m-%d %H:%M{':%S' if seconds else ''} UTC%z")


def describe_notice(value: object, space: str = "", seconds: bool = False) -> str:
    """A notice entry as setup shows it: never the link itself, which is a real message's
    address (config.py doesn't print it either). A link from a workspace other than `space`
    says so: it was kept from before the workspace changed."""
    if not isinstance(value, str):
        return "a value setup can't read (fix it in config.toml)"
    if value.strip().lower() == NO_NOTICE:
        return NONE_SHOWN
    link = NOTICE_LINK.fullmatch(value.strip())
    if link is None:
        return "a value setup can't read (fix it in config.toml)"
    elsewhere = space and not value.strip().startswith(f"{space}/")
    return f"the notice posted {notice_time(link[2], seconds)}" + (
        ", from another workspace" if elsewhere else ""
    )


def linked_channel(value: object) -> str | None:
    """The channel ID in a notice link, or None for "none" or anything else."""
    link = NOTICE_LINK.fullmatch(value.strip()) if isinstance(value, str) else None
    return link[1] if link else None


def same_notice(one: object, other: object) -> bool:
    """Whether two notice values name the same message, as config.py reads them."""
    a = NOTICE_LINK.fullmatch(one.strip()) if isinstance(one, str) else None
    b = NOTICE_LINK.fullmatch(other.strip()) if isinstance(other, str) else None
    return a is not None and b is not None and a.groups() == b.groups()


def check_notice(text: str, entry: str, space: str, others: dict[str, str]) -> str:
    """A typed notice answer: "none", or a Slack message link from this workspace and this
    channel. `others` holds the channel ID each other listed channel is known to have (its
    listed ID, or its notice link's channel). Checked offline, from the link's own parts
    (must-refuse 23). A message never quotes the link (must-refuse 21)."""
    value = text.strip()
    if value.lower() == NO_NOTICE:
        return NO_NOTICE
    link = NOTICE_LINK.fullmatch(value)
    if link is None:
        raise SetupError(
            "That isn't a link to a Slack message (https://<workspace>.slack.com/archives/C.../"
            "p...). Use Copy link on the pinned notice itself, not a reply to it."
        )
    if not value.startswith(f"{space}/"):
        raise SetupError(
            f"That link is from another workspace, not {space}. Copy the link of the notice "
            "pinned in this channel."
        )
    linked = link[1]
    if CHANNEL_ID.fullmatch(entry):
        wrong = linked != entry
    else:
        wrong = linked in others.values()
    if wrong:
        raise SetupError(
            "That link is to a message in another channel. Copy the link of the notice pinned "
            "in this channel."
        )
    return value


def notices_step(
    prompter: Prompter,
    channels: list[str],
    space: str,
    table: dict,
    pasted: dict[str, str] | None = None,
) -> dict:
    """Each listed channel's notice (revision 6). Returns slack_notices with only the typed
    answers changed: every other entry, for a listed channel or not, stays (must-refuse 22),
    and Enter writes nothing new (must-refuse 20). A channel whose notice link was pasted at
    the channel question (`pasted`, #156) takes that link, and isn't asked."""
    pasted = pasted or {}
    new = dict(table)
    known: dict[str, str] = {}
    for entry in channels:
        found = notice_entry(new, entry)
        linked = linked_channel(found[1]) if found else None
        if CHANNEL_ID.fullmatch(entry):
            known[entry] = entry
        elif linked:
            known[entry] = linked
    for entry in channels:
        found = notice_entry(new, entry)
        others = {k: v for k, v in known.items() if k != entry}
        if entry in pasted:
            # The same checks as a typed link: this workspace, this channel. They can't fail
            # today (the workspace must match the links', and the link's channel is the
            # entry), so a failure stops setup with nothing written rather than asking again.
            answer = check_notice(pasted[entry], entry, space, others)
            prompter.say(
                f"Notice for {shown_channel(entry)}: {describe_notice(answer, space)}, from the "
                "link you pasted."
            )
        else:
            # A notice keyed by the channel's name counts for a channel listed by ID too.
            kept = old_notice(new, entry)
            enter = f"keep {describe_notice(kept[1], space)}" if kept else "add it later"
            prompter.say(NOTICE_HELP.format(channel=shown_channel(entry)))
            answer = ask_until(
                prompter,
                NOTICE_QUESTION.format(enter=enter),
                lambda t, e=entry, o=others: check_notice(t, e, space, o) if t.strip() else None,
            )
        if answer is None:
            continue
        old_link = linked_channel(found[1]) if found else None
        new[found[0] if found else entry] = answer
        if old_link and not CHANNEL_ID.fullmatch(entry) and linked_channel(answer) == old_link:
            # The same notice was also keyed by the channel's ID (or a link to it): change it
            # too, or the two entries would disagree and the build would stop. Only for a new
            # link to that same channel: a "none" or another channel's link is never written
            # under that ID unseen, and the build stops on the mismatch instead.
            for key, value in list(new.items()):
                if channel_id(key) == old_link and same_notice(value, found[1]):
                    new[key] = answer
        if CHANNEL_ID.fullmatch(entry) and linked_channel(answer) == entry:
            # The other way round: an entry keyed by a name whose notice links to this
            # channel would disagree with the new one, and the export would stop. Change it
            # too; the summary says so (24).
            for key in name_keys(new, entry):
                new[key] = answer
        linked = linked_channel(answer)
        if linked and not CHANNEL_ID.fullmatch(entry):
            known[entry] = linked
        elif not CHANNEL_ID.fullmatch(entry):
            known.pop(entry, None)
    return new


def name_keys(table: dict, entry: str) -> list[str]:
    """The slack_notices keys that are channel names, whose value is a notice link to the
    channel with this ID."""
    return [
        k
        for k, v in table.items()
        if channel_id(k) is None
        and CHANNEL_NAME.fullmatch(channel_name(k))
        and linked_channel(v) == entry
    ]


def missing_notices(new: dict) -> list[str]:
    """The listed channels with no slack_notices entry."""
    sources = new["sources"]
    table = sources.get(NOTICES_KEY, {})
    return [e for e in sources["slack_channels"] if old_notice(table, e) is None]


def with_answers(
    raw: dict,
    channels: list[str],
    space: str,
    folder: str,
    values: dict[str, str],
    handle: str,
    notices: dict,
    send_not_approved: str,
) -> dict:
    """`raw` with only setup's keys changed (must-refuse 3)."""
    new = deepcopy(raw)
    new_sources = new.setdefault("sources", {})
    new_sources["slack_channels"] = channels
    new_sources["slack_workspace_url"] = space
    if notices or NOTICES_KEY in new_sources:
        new_sources[NOTICES_KEY] = notices
    new.setdefault("local", {})["data_dir"] = folder
    new_store = new.setdefault("store", {})
    new_store["backend"] = "gcs"
    new_store.update(values)
    new_store["producer"] = handle
    new_store["send_not_approved"] = send_not_approved
    return new


def not_approved_step(prompter: Prompter, store: dict, written: bool = True) -> str:
    """send_not_approved, asked with an explicit y or n (#150, item 5; must-refuse 25 to 27).
    Enter alone asks again, on a re-run too: the current value is shown, never kept by itself."""
    prompter.say(NOT_APPROVED_HELP)
    if "send_not_approved" in store:
        where = "Your config.toml says" if written else "You answered"
        prompter.say(f"{where}: {printable(str(store['send_not_approved']))}.")
    return "yes" if prompter.yes(NOT_APPROVED_QUESTION) else "no"


def quick_step(
    prompter: Prompter,
    team: TeamFile | None,
    picker: Callable[[str], Path | None],
    choose: Callable[[str], str],
) -> tuple[dict, TeamFile]:
    """A first run (revision 5): ask only what setup can't choose, and choose the rest. The
    summary screen shows it all before anything is written (must-refuse 16 and 19)."""
    if team is None:
        team = ask_team_file(prompter, picker)
    try:
        shown = data_dir(DEFAULT_DATA_DIR)
        if shown.exists() and not shown.is_dir():
            raise SetupError(f"{shown} is a file, not a folder.")
        folder = DEFAULT_DATA_DIR
    except (UnsafeDataDirError, SetupError):
        # folder_step says why, and asks.
        folder = folder_step(prompter, picker, DEFAULT_DATA_DIR)
    channels, space, pasted = channels_and_workspace(prompter, "", "", always_ask=False)
    notices = notices_step(prompter, channels, space, {}, pasted)
    send = not_approved_step(prompter, {})
    handle = new_handle(team.values["project"], choose)
    return with_answers({}, channels, space, folder, team.values, handle, notices, send), team


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
            "(chmod 600)?"
        ):
            os.chmod(key, 0o600)


def same_json(found: bytes, wanted: bytes) -> bool:
    """Whether two JSON files hold the same contents, however they're laid out: a key saved
    by hand often has other spacing, or no newline at the end."""
    try:
        return json.loads(found.decode("utf-8")) == json.loads(wanted.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False


def install_file(prompter: Prompter, target: Path, data: bytes, label: str) -> bool:
    """Put one part of the team file in place, readable only by the producer. A file that's
    there already is replaced only on a typed yes, and never written through a link. True if
    the team file's part is now in place (written, or the same one was there)."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        prompter.say(f"{target} is a link or a folder, so the {label} wasn't put there.")
        return False
    if target.exists():
        if same_json(target.read_bytes(), data):
            prompter.say(f"The {label} at {target} is the same as the team file's.")
            return True
        if not prompter.yes(
            f"There's a different {label} at {target}. Replace it with the team file's?"
        ):
            prompter.say(f"Kept the {label} that was there.")
            return False
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
            return False
        try:
            with os.fdopen(out, "wb") as file:
                file.write(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        restrict(target)
    prompter.say(f"Saved the {label} at {target}, readable only by you.")
    return True


def next_lines(repo: Path | None = None, windows: bool | None = None) -> list[str]:
    """What to type after day 7: go to the repo folder, then run the export. Quoted for
    PowerShell on Windows, and for a POSIX shell elsewhere."""
    repo = repo or REPO
    windows = sys.platform == "win32" if windows is None else windows
    folder = printable(repo)
    quoted = "'" + folder.replace("'", "''") + "'" if windows else shlex.quote(folder)
    return [f"  cd {quoted}", "  uv run diffdata export"]


def read_back(target: Path, data: bytes) -> bool:
    """Whether the file at `target` is one plain file that reads back as `data`."""
    try:
        found = push.read_plain(target.parent, target.name)
    except OSError:
        return False
    return found is not None and same_json(found, data)


def still_the_team_file(team: TeamFile) -> bool:
    """Whether the path setup was given is still one plain file, not a link, a folder or a
    file with a second hard link, holding the store key setup saved."""
    try:
        if push.is_link(team.path) or push.is_link(team.path.parent) or not team.path.is_file():
            return False
        found = push.read_plain(team.path.parent, team.path.name)
    except OSError:  # a folder can't be opened as a file on every system
        return False
    if found is None:
        return False
    try:
        key = json.loads(found.decode("utf-8")).get("store_key")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return False
    return key is not None and same_json((json.dumps(key) + "\n").encode("utf-8"), team.store_key)


def shown_home(folder: Path) -> str:
    """The settings folder, as ~/.diffdata when it's the usual one."""
    try:
        usual = folder.absolute() == (Path.home() / ".diffdata").absolute()
    except RuntimeError:  # no home folder
        usual = False
    return "~/.diffdata" if usual else printable(folder)


def team_file_place(path: Path) -> str:
    """Where the team file was: "from Downloads", or its path."""
    try:
        downloads = path.absolute().parent == (Path.home() / "Downloads").absolute()
    except RuntimeError:
        downloads = False
    return "from Downloads" if downloads else f"at {printable(path)}"


def install_team_file(prompter: Prompter, folder: Path, team: TeamFile) -> None:
    missing = []
    key = folder / push.KEY_NAME
    if not install_file(prompter, key, team.store_key, "store key") or not read_back(
        key, team.store_key
    ):
        missing.append("store key")
    client = folder / CLIENT_NAME
    if team.google_client is not None and (
        not install_file(prompter, client, team.google_client, "Google client file")
        or not read_back(client, team.google_client)
    ):
        missing.append("Google client file")
    if missing:
        # It may be the producer's only copy of the team's key: never deleted until every
        # part of it is saved and read back.
        prompter.say(
            f"Kept the team file at {printable(team.path)}, because its {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not in place."
        )
        return
    saved = f"Its key is saved in {shown_home(folder)}."
    if not still_the_team_file(team):
        prompter.say(
            f"Kept the team file at {printable(team.path)}: it's no longer one plain file "
            f"holding this key, so setup doesn't delete it. {saved} Delete the team file yourself."
        )
        return
    try:
        team.path.unlink()
    except OSError as e:
        prompter.say(
            f"Couldn't delete the team file {team_file_place(team.path)} "
            f"({e.strerror or type(e).__name__}). {saved} Delete the team file yourself."
        )
        return
    prompter.say(f"Deleted the team file {team_file_place(team.path)}. {saved}")


def check_existing(prompter: Prompter, settings: Path) -> None:
    try:
        push.settings(load_config(settings))
    except (ConfigError, UnsafeDataDirError, push.PushError) as e:
        prompter.say(f"Your config.toml doesn't pass every check yet: {e}")
    else:
        prompter.say("Your config.toml passes diffdata's checks.")


def old_notice(before: dict, entry: str) -> tuple[str, object] | None:
    """This channel's entry in `before`; for a channel listed by ID with no entry under it, an
    entry keyed by a name whose notice links to it."""
    found = notice_entry(before, entry)
    if found is None and CHANNEL_ID.fullmatch(entry):
        keys = name_keys(before, entry)
        found = (keys[0], before[keys[0]]) if keys else None
    return found


def notice_entry_key(table: dict, entry: str) -> str | None:
    found = notice_entry(table, entry)
    return found[0] if found else None


def was_notice(before: dict, after: dict, entry: str, space: str) -> str:
    """What the summary adds after a channel's notice on a re-run (#156, must-refuse 4): the
    old notice, if it changes, and the old one under each name key that changes with it.
    Two notices from the same minute are shown to the second."""
    parts: list[str] = []
    old = old_notice(before, entry)
    new = old_notice(after, entry)
    if changed_notice(before, after, entry):
        was = describe_notice(old[1], space) if old else NOT_ADDED
        if old and new and was == describe_notice(new[1], space):
            was = describe_notice(old[1], space, seconds=True)
            was += f", now {describe_notice(new[1], space, seconds=True)}"
        parts.append(f"was: {was}")
    if CHANNEL_ID.fullmatch(entry):
        for key in name_keys(after, entry):
            if old and key == old[0]:
                continue
            if key in before and not same_notice(before[key], after[key]):
                parts.append(
                    f"under the name {printable(key)} was: {describe_notice(before[key], space)}"
                )
    return f" ({'; '.join(parts)})" if parts else ""


def changed_notice(before: dict, after: dict, entry: str) -> bool:
    """Whether this channel's notice differs between two slack_notices tables."""
    old = old_notice(before, entry)
    new = old_notice(after, entry)
    if old is None or new is None:
        return (old is None) != (new is None)
    if same_notice(old[1], new[1]):
        return False
    words = [v.strip().lower() if isinstance(v, str) else v for v in (old[1], new[1])]
    return words[0] != words[1]


def show_summary(
    prompter: Prompter,
    new: dict,
    team: TeamFile | None,
    settings: Path,
    first: bool,
    before: dict | None = None,
) -> None:
    """Everything setup will write. Nothing from the key or the client file is shown. On a
    re-run, `before` is config.toml's slack_notices: a channel whose notice changes shows the
    old one too (#156, must-refuse 4)."""
    store = new["store"]
    prompter.say("")
    prompter.say(f"Setup will write this to {settings}:" if first else "You chose:")
    channels = new["sources"]["slack_channels"]
    prompter.say(f"  Channels:         {', '.join(shown_channel(e) for e in channels)}")
    table = new["sources"].get(NOTICES_KEY, {})
    for number, entry in enumerate(channels):
        found = notice_entry(table, entry)
        space = new["sources"]["slack_workspace_url"]
        notice = describe_notice(found[1], space) if found else NOT_ADDED
        linked = linked_channel(found[1]) if found and not CHANNEL_ID.fullmatch(entry) else None
        if linked and any(
            key != found[0] and channel_id(key) == linked and same_notice(value, found[1])
            for key, value in table.items()
        ):
            # Written under the channel's ID too (notices_step): on the screen as well (24).
            notice += ", also under its channel ID"
        if CHANNEL_ID.fullmatch(entry):
            # Listed by ID: a notice keyed by the channel's name counts too, as the export
            # reads it, so keeping one never looks like removing it.
            found = old_notice(table, entry)
            notice = describe_notice(found[1], space) if found else NOT_ADDED
            if found and found[0] != notice_entry_key(table, entry):
                notice += f", under the name {printable(found[0])}"
            names = [
                k
                for k in name_keys(table, entry)
                if found and k != found[0] and same_notice(table[k], found[1])
            ]
            if names:
                notice += f", also under the name {', '.join(printable(k) for k in names)}"
        if before is not None:
            notice += was_notice(before, table, entry, space)
        label = "  Notices:         " if number == 0 else "                   "
        prompter.say(f"{label} {shown_channel(entry)}: {notice}")
    prompter.say(f"  Workspace:        {new['sources']['slack_workspace_url']}")
    prompter.say(f"  Data folder:      {data_dir(new['local']['data_dir'])}")
    prompter.say(
        f"  Team values:      bucket {store['bucket']}, folder {store['folder']} (the project "
        f"code), project {store['project']}"
    )
    if team is not None:
        parts = "a store key" + (" and a Google client file" if team.google_client else "")
        prompter.say(
            f"                    (from the team file {printable(team.path)}, "
            f"which also holds {parts})"
        )
    prompter.say(f"  Producer handle:  {store['producer']} (a pseudonym)")
    sent = (
        "yes: sent to the not-approved part of your team's folder, never given to consumers"
        if store.get("send_not_approved") == "yes"
        else "no: push sends none of them"
    )
    prompter.say(f"  Not approved:     {sent}")


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
    # Decided once, before any question: a config that goes away while setup waits never
    # turns a re-run into a first run (which would drop its other settings).
    first = not settings.exists()
    if not first:
        raw = read_raw(settings)
        prompter.say(f"You have a config.toml already, at {settings}.")
        if not prompter.yes("Change it?"):
            check_existing(prompter, settings)
            if team is not None:
                prompter.say(
                    "So the team file's team values weren't used. To use them, run setup again "
                    "and answer y to Change it."
                )
                values = team.values
                prompter.say(
                    f"The team file is for bucket {values['bucket']}, folder {values['folder']}, "
                    f"project {values['project']}."
                )
                store = raw.get("store", {})
                if all(str(store.get(key, "")) == values[key] for key in TEAM_KEYS):
                    prompter.say("These match your config.toml.")
                else:
                    prompter.say(
                        "These don't match the team values in your config.toml, so push will "
                        "fail with this team file's key. To fix it, run setup again with this "
                        "team file and answer y to Change it."
                    )
                parts = "store key" + (" and Google client file" if team.google_client else "")
                if prompter.yes(f"Put the team file's {parts} in place anyway?"):
                    try:
                        install_team_file(prompter, folder, team)
                    except OSError as e:
                        raise SetupError(
                            f"Couldn't put the team file's parts in place: {e}"
                        ) from None
            key_step(prompter, folder)
            return 0
    if first:
        new, team = quick_step(prompter, team, picker, choose)
    else:
        new, team = config_step(prompter, team, raw, picker, choose)
    old_sources = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    before = None if first else notice_table(old_sources)
    while True:
        show_summary(prompter, new, team, settings, first, before)
        if not first:
            if prompter.yes(f"Write this to {settings}?"):
                break
            prompter.say(NOTHING_WRITTEN)
            return 0
        prompter.say(PSEUDONYM)
        answer = prompter.letter(
            "Write this? Type y to write it, n to stop, or c to change something", "ync"
        )
        if answer == "y":
            break
        if answer == "n":
            prompter.say(NOTHING_WRITTEN)
            return 0
        # c: each question in turn, with the shown values as the defaults; then the screen again.
        new, team = config_step(prompter, team, new, picker, choose, written=False)
        # A first run has no earlier notices: one for a channel taken off the list after c
        # would be written without being on the screen (must-refuse 24).
        table = new["sources"].get(NOTICES_KEY, {})
        listed = new["sources"]["slack_channels"]
        kept = {k: v for k, v in table.items() if (channel_id(k) or channel_name(k)) in listed}
        if kept:
            new["sources"][NOTICES_KEY] = kept
        else:
            new["sources"].pop(NOTICES_KEY, None)
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
    missing = missing_notices(new)
    if missing:
        prompter.say(
            f"No notice link yet for: {', '.join(shown_channel(e) for e in missing)}. "
            "/slack-export stops until each listed channel has one. When the notice is "
            "posted, run `uv run diffdata setup` again, answer y to Change it, and paste "
            "the link."
        )
    else:
        prompter.say(NEXT_STEP)
        for line in next_lines():
            prompter.say(line)
    return 0
