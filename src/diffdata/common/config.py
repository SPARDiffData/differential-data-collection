"""Read config.toml, each person's private settings.

config.toml stays on the user's computer, in ~/.diffdata/ so that tools working in the
repo, such as Codex, can't read it. A config.toml in the current folder still works; it's
gitignored. The data folder always comes through `data_dir()`, so a folder inside a repo
or a synced folder is refused.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from diffdata.common.paths import data_dir

CONFIG_NAME = "config.toml"


class ConfigError(Exception):
    """config.toml is missing, unreadable, or doesn't have a setting we need."""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # CON-1: listing a channel is the opt-in to collecting it. Names, without the '#'.
    slack_channels: tuple[str, ...]
    # The workspace's address, like https://example.slack.com, for message links.
    slack_workspace_url: str = ""
    # Which config.toml these settings came from.
    path: Path | None = None


def channel_name(name: str) -> str:
    """Slack channel names are lowercase; people sometimes type the '#'."""
    return name.strip().lstrip("#").lower()


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

    return Config(
        data_dir=data_dir(configured),
        slack_channels=tuple(channel_name(c) for c in channels),
        slack_workspace_url=workspace.rstrip("/"),
        path=path,
    )
