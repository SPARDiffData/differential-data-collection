"""`diffdata check` (ENG-1, plan #136): what a producer needs, checked from the terminal.

It prints one line per item: OK, or what's wrong and how to fix it. `diffdata setup` runs the
tool and folder checks first, and lists any gap at the end, under "Before you export".

What it refuses (#136, approved on 2026-11-25):
  1  to change anything: it only reads. It runs `git --version` and `claude --version`,
     nothing else, with no network call (uv is only looked for on PATH, not run)
  2  to read or print the store key's contents, or any team file: it looks at the key's
     place and file mode only
  3  to stop setup for a missing tool setup doesn't need

Not checked: Claude's Slack connector and a Google account, since a terminal can't see
Claude's connectors. The README's "Before you start" list covers them.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from diffdata.common import config as config_module
from diffdata.common.config import CHANNEL_ID, Config, ConfigError, load_config
from diffdata.common.paths import UnsafeDataDirError, find_git_root, synced_by

CLAUDE_MIN = (2, 1, 295)
CLAUDE_MIN_SHOWN = ".".join(map(str, CLAUDE_MIN))
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TIMEOUT = 15
KEY_NAME = "store-key.json"  # the same name as store/push.py's KEY_NAME (tested)

GIT_FIX = (
    "Git isn't installed, or isn't on your PATH: see https://git-scm.com/downloads (on a "
    "Mac, run `git --version` in Terminal and accept the install prompt)."
)
UV_FIX = (
    "uv isn't on your PATH: see https://docs.astral.sh/uv/getting-started/installation/, "
    "then open a new terminal."
)
CLAUDE_MISSING = (
    "Claude Code isn't installed: neither the `claude` command nor the Claude desktop app was "
    "found. See https://claude.com/claude-code. /slack-export runs in it."
)
REPO_FIX = (
    "This isn't the repo folder. Run diffdata from your clone of differential-data-collection, "
    "so /slack-export runs with the repo's safety rules (.claude/settings.json)."
)

# Runs one `<tool> --version` and returns its output, or None if it didn't run.
Runner = Callable[[str], str | None]
# The newest Claude Code the Claude desktop app has, or None if there's no app.
Desktop = Callable[[], tuple[int, int, int] | None]


@dataclass(frozen=True)
class Item:
    name: str
    ok: bool
    detail: str


def run_version(tool: str) -> str | None:
    """`<tool> --version`, found on PATH. None if it isn't there, fails or hangs. It runs in
    a thread that is left behind after TIMEOUT, so a wrapper that keeps its output open (a
    Windows .cmd's child, say) can't hang setup."""
    found = shutil.which(tool)
    if found is None:
        return None
    if tool == "claude" and "windowsapps" in found.lower():
        # The Windows app alias opens the desktop app instead of printing a version.
        return None
    result: list[str | None] = []

    def probe() -> None:
        try:
            done = subprocess.run(
                [found, "--version"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            result.append(None)
            return
        ok = done.returncode == 0
        result.append((done.stdout or done.stderr).strip() if ok else None)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(TIMEOUT + 2)
    return result[0] if result else None


def desktop_claude_code() -> tuple[int, int, int] | None:
    """The newest Claude Code the Claude desktop app keeps, from its folder names only. The
    app runs /slack-export too, with no `claude` command on PATH."""
    places: list[Path] = []
    if sys.platform == "darwin":
        places.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude-code")
    elif sys.platform == "win32":
        if os.environ.get("APPDATA"):
            places.append(Path(os.environ["APPDATA"]) / "Claude" / "claude-code")
        if os.environ.get("LOCALAPPDATA"):
            # The Microsoft Store (MSIX) install keeps its data in a package folder.
            local = Path(os.environ["LOCALAPPDATA"])
            store = local / "Claude-Data"
            try:
                places += [package / "claude-code" for package in store.iterdir()]
            except OSError:
                pass
            try:
                places += [
                    package / "LocalCache" / "Roaming" / "Claude" / "claude-code"
                    for package in (local / "Packages").glob("Claude_*")
                ]
            except OSError:
                pass
    found: list[tuple[int, int, int]] = []
    for place in places:
        try:
            for entry in place.iterdir():
                # A version folder with something in it: an empty one is a half-done update.
                if VERSION.fullmatch(entry.name) and entry.is_dir() and any(entry.iterdir()):
                    version = parse_version(entry.name)
                    if version:
                        found.append(version)
        except OSError:
            continue
    return max(found) if found else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first x.y.z in `text`. A pre-release (x.y.z-rc.1) reads as the version before it,
    so it never passes as its release."""
    found = VERSION.search(text)
    if not found:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    if text[found.end() : found.end() + 1] == "-":
        if patch:
            return (major, minor, patch - 1)
        return (major, minor - 1, 999_999) if minor else (major - 1, 999_999, 999_999)
    return (major, minor, patch)


def first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def tool_items(runner: Runner = run_version, desktop: Desktop = desktop_claude_code) -> list[Item]:
    """Python, uv, Git and Claude Code (the `claude` command, or the desktop app's)."""
    items = [Item("Python", True, sys.version.split()[0])]
    uv = shutil.which("uv")
    items.append(Item("uv", True, uv) if uv else Item("uv", False, UV_FIX))
    git = runner("git")
    items.append(Item("Git", True, first_line(git)) if git else Item("Git", False, GIT_FIX))
    items.append(claude_item(runner("claude"), desktop))
    return items


def claude_item(claude: str | None, desktop: Desktop) -> Item:
    """The `claude` command if it's new enough, else the desktop app's Claude Code."""
    if claude:
        # A wrapper may print other versions first (Node's, say): use Claude Code's line.
        named = [line for line in claude.splitlines() if "claude" in line.lower()]
        version = parse_version(named[0] if named else claude)
    else:
        version = None
    if version is not None and version >= CLAUDE_MIN:
        return Item("Claude Code", True, ".".join(map(str, version)))
    app = desktop()
    if app is not None and app >= CLAUDE_MIN:
        detail = f"{'.'.join(map(str, app))}, in the Claude desktop app"
        if version is not None:
            detail += (
                f" (the `claude` command is {'.'.join(map(str, version))}: run `claude update`)"
            )
        return Item("Claude Code", True, detail)
    if version is not None:
        return Item(
            "Claude Code",
            False,
            f"Claude Code {'.'.join(map(str, version))} is older than {CLAUDE_MIN_SHOWN}, which "
            "/slack-export needs: run `claude update`.",
        )
    if claude is not None:
        return Item(
            "Claude Code",
            False,
            "Couldn't read Claude Code's version from `claude --version`. /slack-export needs "
            f"{CLAUDE_MIN_SHOWN} or later.",
        )
    if app is not None:
        return Item(
            "Claude Code",
            False,
            f"The Claude desktop app has Claude Code {'.'.join(map(str, app))}, older than "
            f"{CLAUDE_MIN_SHOWN}, which /slack-export needs: update the app.",
        )
    return Item("Claude Code", False, CLAUDE_MISSING)


def has_rules(settings_json: Path) -> bool:
    """The repo's .claude/settings.json, with its deny rules on Slack's write tools: not an
    empty or edited-away one. Read only if it's a small plain file with one link, so a link
    to a key or team file, or a pipe, is never read."""
    try:
        info = settings_json.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000:
            return False
        rules = json.loads(settings_json.read_text(encoding="utf-8"))
        deny = rules["permissions"]["deny"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return isinstance(deny, list) and any("slack_send" in str(rule).lower() for rule in deny)


def repo_item(folder: Path | None = None) -> Item:
    """The current folder is in a clone of this repo, with its safety rules."""
    here = (folder or Path.cwd()).resolve()
    root = find_git_root(here)
    if root is not None:
        rules = root / ".claude" / "settings.json"
        export = root / ".claude" / "commands" / "slack-export.md"
        if has_rules(rules) and export.is_file():
            return Item("Repo folder", True, str(root))
    return Item("Repo folder", False, REPO_FIX)


def state_items(settings: Path | None = None) -> list[Item]:
    """config.toml and the store key. The key is looked at, never opened (must-refuse 2)."""
    settings = settings or config_module.user_config_file()
    items: list[Item] = []
    if settings.is_symlink():
        items.append(
            Item(
                "config.toml",
                False,
                f"{settings} is a link: save config.toml there as a plain file. (Not opened.)",
            )
        )
    elif settings.is_file() and settings.stat().st_nlink != 1:
        items.append(
            Item(
                "config.toml",
                False,
                f"{settings} has a second hard link: save config.toml there as a plain file. "
                "(Not opened.)",
            )
        )
    elif not settings.is_file():
        items.append(
            Item(
                "config.toml",
                False,
                f"There's no config.toml at {settings}: run `uv run diffdata setup <team file>`.",
            )
        )
    else:
        try:
            config = load_config(settings)
        except (ConfigError, UnsafeDataDirError) as e:
            items.append(Item("config.toml", False, f"{settings} doesn't pass its checks: {e}"))
        except Exception as e:
            items.append(
                Item(
                    "config.toml",
                    False,
                    f"{settings} doesn't pass its checks: an unexpected {type(e).__name__}. "
                    "Run `uv run diffdata setup` to fix it.",
                )
            )
        else:
            items.append(Item("config.toml", True, str(settings)))
            items += notice_items(config)
    key = settings.parent / KEY_NAME
    try:
        info = key.lstat()
    except FileNotFoundError:
        items.append(
            Item(
                "Store key",
                False,
                f"There's no store key at {key}: it comes in the team file. Run "
                "`uv run diffdata setup <team file>`.",
            )
        )
        return items
    except OSError as e:
        items.append(Item("Store key", False, f"Couldn't look at {key}: {e.strerror}."))
        return items
    if not stat.S_ISREG(info.st_mode):
        items.append(
            Item("Store key", False, f"{key} is a link or a folder: save the key as a plain file.")
        )
    elif any(
        find_git_root(place.parent) is not None or synced_by(place) is not None
        for place in (key.absolute(), key.resolve())
    ):
        items.append(
            Item(
                "Store key",
                False,
                f"{key} is inside a git repository or a synced folder, where it could be shared "
                "by mistake: move ~/.diffdata/ out of it.",
            )
        )
    elif info.st_nlink != 1:
        items.append(
            Item(
                "Store key",
                False,
                f"{key} has a second hard link, so it could also sit in a repository: save it "
                "as a plain file.",
            )
        )
    elif not os.access(key, os.R_OK):
        items.append(Item("Store key", False, f"You can't read {key}: run `chmod 600 {key}`."))
    elif os.name != "nt" and info.st_mode & 0o077:
        items.append(
            Item(
                "Store key",
                False,
                f"Others on this computer can read {key}: run `chmod 600 {key}`.",
            )
        )
    else:
        items.append(Item("Store key", True, str(key)))
    return items


def notice_items(config: Config) -> list[Item]:
    """A FIX line for each listed channel with no slack_notices entry, since the build stops
    at it (#119; plan #130, revision 6). Read from config.toml only: a channel listed by name
    with an entry keyed by its ID can't be matched without Slack, so it reads as missing."""
    items: list[Item] = []
    # Keyed as config.py reads them: a channel's ID (from an ID or a link), or its name.
    notices = dict(config.slack_notices)
    unmatched_ids = any(
        CHANNEL_ID.fullmatch(key) and key not in config.listed_channels for key in notices
    )
    for number, listed in enumerate(config.listed_channels, start=1):
        by_id = CHANNEL_ID.fullmatch(listed) is not None
        if listed not in notices:
            shown = f"{listed}, a channel ID" if by_id else listed
            items.append(
                Item(
                    "Notice",
                    False,
                    f"Channel {number} in slack_channels ({shown}) has no notice link, so "
                    "/slack-export stops at it. Run `uv run diffdata setup`, answer y to Change "
                    "it, and paste the pinned notice's link, or type none if everyone in it "
                    "agreed directly."
                    + (
                        " (An entry keyed by a channel's ID may be this one's: if it is, it's "
                        "fine.)"
                        if unmatched_ids and not by_id
                        else ""
                    ),
                )
            )
    if config.listed_channels and not items:
        items.append(Item("Notices", True, "every listed channel has an entry"))
    return items


def lines(items: list[Item]) -> list[str]:
    width = max(len(item.name) for item in items)
    return [
        f"  {'OK ' if item.ok else 'FIX'}  {item.name.ljust(width)}  {item.detail}"
        for item in items
    ]


def check(
    runner: Runner = run_version,
    desktop: Desktop = desktop_claude_code,
    folder: Path | None = None,
) -> int:
    """The whole of `diffdata check`. Returns 0 if every item is OK, else 1."""
    items = [*tool_items(runner, desktop), repo_item(folder), *state_items()]
    print("diffdata check:")
    for line in lines(items):
        print(line)
    missing = [item for item in items if not item.ok]
    print("")
    if missing:
        print(f"{len(missing)} to fix. The Slack connector and a Google account aren't checked:")
    else:
        print("All set. The Slack connector and a Google account aren't checked:")
    print('see "Before you start" in the README.')
    return 1 if missing else 0


def before_export(items: list[Item]) -> list[str]:
    """What setup shows at the end: the gaps, or nothing if there are none."""
    missing = [item for item in items if not item.ok]
    if not missing:
        return []
    return ["", "Before you export:", *lines(missing)]
