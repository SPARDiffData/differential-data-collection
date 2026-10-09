"""Where local data lives, and the guards that keep it local.

HARD RULE: real collected data never goes in a git commit. Every stage gets its
data folder from `data_dir()`, which refuses any folder inside a git repository.
It also refuses folders that a cloud service syncs, which would quietly copy
private data off this computer.
"""

from pathlib import Path


class UnsafeDataDirError(Exception):
    """The configured data folder is somewhere collected data must not go."""


class DataInRepoError(UnsafeDataDirError):
    """The configured data folder is inside a git repository."""


class DataInSyncedFolderError(UnsafeDataDirError):
    """The configured data folder is inside a folder that a cloud service syncs."""


# Folder names that cloud sync apps create, matched case-insensitively against each part
# of the path. This is a best-effort check on names: it can't see every setup, such as
# macOS iCloud syncing the whole Desktop and Documents folders.
SYNCED_NAME_PREFIXES = {
    "onedrive": "OneDrive",  # "OneDrive", "OneDrive - Org", macOS "OneDrive-Org"
    "dropbox": "Dropbox",  # "Dropbox", "Dropbox (Org)"
    "googledrive": "Google Drive for desktop",  # macOS CloudStorage/GoogleDrive-<account>
}
SYNCED_NAMES = {
    "my drive": "Google Drive for desktop",  # Windows G:\My Drive, or mirrored ~/My Drive
    "shared drives": "Google Drive for desktop",
    "google drive": "Google Drive for desktop",
    "iclouddrive": "iCloud Drive",  # Windows: C:\Users\<you>\iCloudDrive
    "icloud drive": "iCloud Drive",
    "mobile documents": "iCloud Drive",  # macOS: ~/Library/Mobile Documents/com~apple~CloudDocs
    "cloudstorage": "a cloud storage app",  # macOS: ~/Library/CloudStorage/<any sync app>
}


def find_git_root(path: Path) -> Path | None:
    """Return the git repository that contains `path`, or None if there isn't one."""
    for candidate in [path, *path.parents]:
        # `.git` is a folder in a normal clone and a file in worktrees and submodules.
        if (candidate / ".git").exists():
            return candidate
    return None


def synced_by(path: Path) -> str | None:
    """Name the cloud service that syncs `path`, or None if none of its folder names match."""
    for part in path.parts:
        name = part.lower()
        if name in SYNCED_NAMES:
            return SYNCED_NAMES[name]
        for prefix, service in SYNCED_NAME_PREFIXES.items():
            if name.startswith(prefix):
                return service
    return None


def data_dir(configured: str | Path) -> Path:
    """Resolve the configured data folder, refusing a git repository or a synced folder."""
    path = Path(configured).expanduser().resolve()
    repo = find_git_root(path)
    if repo is not None:
        raise DataInRepoError(
            f"The data folder {path} is inside the git repository at {repo}. "
            "Real collected data must never be committed, so set data_dir in "
            "config.toml to a folder outside any repository."
        )
    service = synced_by(path)
    if service is not None:
        raise DataInSyncedFolderError(
            f"The data folder {path} looks like it's synced by {service}. "
            "Collected data must stay on this computer, so set data_dir in "
            "config.toml to a folder that no cloud service syncs."
        )
    return path
