"""Guards for the hard rule: real collected data never goes in a git commit."""

import re
import subprocess
from pathlib import Path

import pytest

from diffdata.common.paths import DataInRepoError, data_dir

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DATA_FOLDERS = {"data", "output", "exports"}
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net"}


def test_data_dir_inside_a_repo_is_refused(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(DataInRepoError):
        data_dir(tmp_path / "repo" / "collected")


def test_data_dir_inside_this_repo_is_refused():
    with pytest.raises(DataInRepoError):
        data_dir(ROOT / "data")


def test_data_dir_outside_a_repo_is_allowed(tmp_path):
    assert data_dir(tmp_path / "collected") == (tmp_path / "collected").resolve()


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


def emails_outside_example_domains(folder: Path) -> list[str]:
    """List emails in `folder`'s text files whose domain isn't example.com/.org/.net."""
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary fixtures (a sample .docx or recording) can't be scanned as text
        found += [
            f"{path.name}: {domain}"
            for domain in EMAIL.findall(text)
            if domain.lower() not in EXAMPLE_DOMAINS
        ]
    return found


def test_fixture_emails_use_reserved_example_domains():
    # Synthetic data only: fake people get addresses at example.com/.org/.net (RFC 2606).
    assert emails_outside_example_domains(FIXTURES) == [], "fixtures must be synthetic"


def test_email_check_skips_binary_files_and_catches_other_domains(tmp_path):
    (tmp_path / "sample.docx").write_bytes(b"PK\x03\x04\xff\xfe binary")
    (tmp_path / "notes.md").write_text("Ask sam@company.invalid", encoding="utf-8")
    assert emails_outside_example_domains(tmp_path) == ["notes.md: company.invalid"]
