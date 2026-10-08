"""Guards for the hard rule: real collected data never goes in a git commit."""

import re
import subprocess
from pathlib import Path

import pytest

from diffdata.common.paths import DataInRepoError, data_dir

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DATA_FOLDERS = {"data", "output", "exports"}


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


def test_fixture_emails_use_reserved_example_domains():
    # Synthetic data only: fake people get addresses at example.com/.org/.net (RFC 2606).
    email = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            for domain in email.findall(path.read_text(encoding="utf-8")):
                assert domain.lower() in {"example.com", "example.org", "example.net"}, (
                    f"{path.name} has an email at {domain}; fixtures must be synthetic"
                )
