"""Where local data lives, and the guard that keeps it out of git.

HARD RULE: real collected data never goes in a git commit. Every stage gets its
data folder from `data_dir()`, which refuses any folder inside a git repository.
"""

from pathlib import Path


class DataInRepoError(Exception):
    """The configured data folder is inside a git repository."""


def find_git_root(path: Path) -> Path | None:
    """Return the git repository that contains `path`, or None if there isn't one."""
    for candidate in [path, *path.parents]:
        # `.git` is a folder in a normal clone and a file in worktrees and submodules.
        if (candidate / ".git").exists():
            return candidate
    return None


def data_dir(configured: str | Path) -> Path:
    """Resolve the configured data folder, refusing any folder inside a git repository."""
    path = Path(configured).expanduser().resolve()
    repo = find_git_root(path)
    if repo is not None:
        raise DataInRepoError(
            f"The data folder {path} is inside the git repository at {repo}. "
            "Real collected data must never be committed, so set data_dir in "
            "config.toml to a folder outside any repository."
        )
    return path
