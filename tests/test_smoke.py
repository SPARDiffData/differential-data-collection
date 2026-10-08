"""Smoke test: the package installs, imports, and its command runs."""

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import diffdata
from diffdata import cli

ROOT = Path(__file__).resolve().parents[1]
STAGES = ["consent", "discover", "collect", "organize", "scrub", "store", "common"]


def test_version_matches_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert diffdata.__version__ == pyproject["project"]["version"]


@pytest.mark.parametrize("stage", STAGES)
def test_stage_package_imports(stage):
    importlib.import_module(f"diffdata.{stage}")


@pytest.mark.parametrize("command", sorted(cli.COMMANDS))
def test_stub_command_says_not_built_yet(command, capsys):
    assert cli.main([command]) == 1
    assert "not built yet" in capsys.readouterr().err


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: diffdata" in capsys.readouterr().out


def test_python_dash_m_works():
    result = subprocess.run(
        [sys.executable, "-m", "diffdata", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"diffdata {diffdata.__version__}"


def test_config_example_parses():
    config = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    assert {"sources", "local", "store"} <= config.keys()
