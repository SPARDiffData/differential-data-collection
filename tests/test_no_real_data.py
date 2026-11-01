"""Guards for the hard rule: real collected data never goes in a git commit."""

import re
import subprocess
from pathlib import Path

import pytest

from diffdata.common.paths import DataInRepoError, DataInSyncedFolderError, data_dir

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DATA_FOLDERS = {"data", "output", "exports"}
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net"}
# Fixture types the email check can't read as text. Check these by hand before committing.
BINARY_SUFFIXES = {
    ".docx",
    ".xlsx",
    ".pptx",
    ".pdf",
    ".png",
    ".jpg",
    ".mp3",
    ".m4a",
    ".mp4",
    ".wav",
}
UNREADABLE = (
    "not UTF-8, so it can't be checked. If it's meant to be binary, add its type to "
    "BINARY_SUFFIXES and check the file by hand. If it's text, save it as UTF-8."
)


def test_data_dir_inside_a_repo_is_refused(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(DataInRepoError):
        data_dir(tmp_path / "repo" / "collected")


def test_data_dir_inside_this_repo_is_refused():
    with pytest.raises(DataInRepoError):
        data_dir(ROOT / "data")


def test_data_dir_outside_a_repo_is_allowed(tmp_path):
    assert data_dir(tmp_path / "collected") == (tmp_path / "collected").resolve()


@pytest.mark.parametrize(
    "synced",
    [
        "OneDrive/collected",
        "OneDrive - Example Org/collected",
        "Dropbox (Example)/collected",
        "My Drive/collected",  # Google Drive for desktop on Windows, or mirrored
        "Library/CloudStorage/GoogleDrive-alex@example.com/My Drive/collected",
        "iCloudDrive/collected",  # iCloud on Windows
        "Library/Mobile Documents/com~apple~CloudDocs/collected",  # iCloud Drive on macOS
    ],
)
def test_data_dir_in_a_synced_folder_is_refused(tmp_path, synced):
    with pytest.raises(DataInSyncedFolderError):
        data_dir(tmp_path / synced)


def test_no_env_config_or_data_files_are_tracked():
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not running inside a git checkout")

    def forbidden(path: str) -> bool:
        parts = path.split("/")
        name = parts[-1]
        return (
            name == "config.toml"
            or (name.startswith(".env") and name != ".env.example")
            or bool(DATA_FOLDERS & set(parts[:-1]))
        )

    assert [p for p in tracked if forbidden(p)] == []


def stray_env_files(folder: Path) -> list[str]:
    """Name the .env files in `folder`, other than .env.example. Never opens them."""
    return sorted(
        path.name
        for path in folder.iterdir()
        if (path.name == ".env" or path.name.startswith(".env."))
        and path.name != ".env.example"
        and path.is_file()
    )


def test_no_env_file_in_the_repo_folder():
    # .env lives in ~/.diffdata/, where tools working in the repo, such as Codex, can't read it.
    stray = stray_env_files(ROOT)
    assert stray == [], (
        f"Move {', '.join(stray)} from {ROOT} to {Path.home() / '.diffdata'}, next to config.toml."
    )


def test_stray_env_files_finds_env_but_not_the_example(tmp_path):
    for name in [".env", ".env.local", ".env.example", "env.txt"]:
        (tmp_path / name).write_text("FAKE_TOKEN=\n", encoding="utf-8")
    assert stray_env_files(tmp_path) == [".env", ".env.local"]
    # A folder isn't a settings file: some people name a Python environment folder .env.
    other = tmp_path / "other"
    (other / ".env").mkdir(parents=True)
    assert stray_env_files(other) == []


def fixture_email_problems(folder: Path) -> list[str]:
    """List emails outside example.com/.org/.net, and text files too unreadable to check."""
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Don't skip it: a CSV saved from Excel could hide real addresses this way.
            found.append(f"{path.name}: {UNREADABLE}")
            continue
        found += [
            f"{path.name}: {domain}"
            for domain in EMAIL.findall(text)
            if domain.lower() not in EXAMPLE_DOMAINS
        ]
    return found


def test_fixture_emails_use_reserved_example_domains():
    # Synthetic data only: fake people get addresses at example.com/.org/.net (RFC 2606).
    assert fixture_email_problems(FIXTURES) == [], "see tests/fixtures/README.md"


def test_email_check_skips_binary_types_but_flags_other_encodings(tmp_path):
    (tmp_path / "sample.docx").write_bytes(b"PK\x03\x04\xff\xfe binary")
    (tmp_path / "notes.md").write_text("Ask sam@company.invalid", encoding="utf-8")
    (tmp_path / "people.csv").write_text("José,jose@company.invalid", encoding="cp1252")
    assert fixture_email_problems(tmp_path) == [
        "notes.md: company.invalid",
        "people.csv: not UTF-8, so it can't be checked. If it's meant to be binary, add its type "
        "to BINARY_SUFFIXES and check the file by hand. If it's text, save it as UTF-8.",
    ]
