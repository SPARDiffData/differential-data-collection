"""`diffdata check` (#136). One test or more per must-refuse item.

Every test uses made-up tools, folders and keys in a temporary folder. No test reads the real
~/.diffdata/ or runs the real git, uv or claude.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common import check
from diffdata.common.check import CLAUDE_MISSING, GIT_FIX, REPO_FIX, UV_FIX, Item
from diffdata.store import push

POSIX = pytest.mark.skipif(os.name == "nt", reason="file modes are for macOS and Linux")
MARKER = "MADE-UP-KEY-MARKER"


def runner(**found):
    """A made-up `<tool> --version`: the given output, or None (not installed)."""
    asked: list[str] = []

    def run(tool: str):
        asked.append(tool)
        return found.get(tool)

    run.asked = asked  # type: ignore[attr-defined]
    return run


ALL = {"git": "git version 2.54.0", "claude": "2.1.330 (Claude Code)"}


@pytest.fixture(autouse=True)
def uv_on_path(monkeypatch):
    """uv is looked for on PATH, never run: found, unless a test says otherwise."""
    monkeypatch.setattr(check.shutil, "which", lambda tool: f"/made-up/{tool}")


def no_app():
    return None


def by_name(items: list[Item]) -> dict[str, Item]:
    return {item.name: item for item in items}


# --- tools ---------------------------------------------------------------------------------


def test_every_tool_found_is_ok():
    items = by_name(check.tool_items(runner(**ALL), no_app))
    assert all(item.ok for item in items.values())
    assert items["Claude Code"].detail == "2.1.330"
    assert items["Git"].detail == "git version 2.54.0"


def test_missing_git_says_how_to_install_it():
    item = by_name(check.tool_items(runner(claude=ALL["claude"]), no_app))["Git"]
    assert not item.ok
    assert item.detail == GIT_FIX


def test_uv_is_looked_for_never_run(monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda tool: None)
    run = runner(**ALL)
    item = by_name(check.tool_items(run, no_app))["uv"]
    assert not item.ok
    assert item.detail == UV_FIX
    assert "uv" not in run.asked


def test_no_claude_command_and_no_desktop_app():
    found = {k: v for k, v in ALL.items() if k != "claude"}
    item = by_name(check.tool_items(runner(**found), no_app))["Claude Code"]
    assert not item.ok
    assert item.detail == CLAUDE_MISSING


def test_the_desktop_apps_claude_code_counts():
    found = {k: v for k, v in ALL.items() if k != "claude"}
    item = by_name(check.tool_items(runner(**found), lambda: (2, 1, 331)))["Claude Code"]
    assert item.ok
    assert item.detail == "2.1.331, in the Claude desktop app"


def test_an_old_desktop_app_says_to_update_it():
    found = {k: v for k, v in ALL.items() if k != "claude"}
    item = by_name(check.tool_items(runner(**found), lambda: (2, 1, 200)))["Claude Code"]
    assert not item.ok
    assert "update the app" in item.detail


def test_the_claude_command_wins_over_the_app():
    item = by_name(check.tool_items(runner(**ALL), lambda: (2, 1, 1)))["Claude Code"]
    assert item.ok
    assert item.detail == "2.1.330"


@pytest.mark.parametrize(
    ("output", "ok"),
    [
        ("2.1.295 (Claude Code)", True),
        ("2.1.294 (Claude Code)", False),
        ("2.0.999", False),
        ("3.0.0", True),
    ],
)
def test_claude_code_must_be_new_enough(output, ok):
    item = by_name(check.tool_items(runner(**{**ALL, "claude": output}), no_app))["Claude Code"]
    assert item.ok is ok
    if not ok:
        assert "claude update" in item.detail


def test_a_claude_version_that_cant_be_read():
    item = by_name(check.tool_items(runner(**{**ALL, "claude": "Claude Code"}), no_app))
    assert not item["Claude Code"].ok
    assert "Couldn't read" in item["Claude Code"].detail


def test_must_refuse_1_only_the_three_version_commands_run(monkeypatch):
    run = runner(**ALL)
    check.tool_items(run, no_app)
    assert run.asked == ["git", "claude"]

    calls = []
    monkeypatch.setattr(check.shutil, "which", lambda tool: f"/made-up/{tool}")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="1.2.3\n", stderr="")

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    assert check.run_version("git") == "1.2.3"
    assert calls[0][0] == ["/made-up/git", "--version"]
    assert calls[0][1]["timeout"] == check.TIMEOUT
    assert calls[0][1]["stdin"] == subprocess.DEVNULL


def test_a_tool_not_on_path_or_failing_or_hanging_reads_as_missing(monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda tool: None)
    assert check.run_version("git") is None

    monkeypatch.setattr(check.shutil, "which", lambda tool: f"/made-up/{tool}")
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="boom"),
    )
    assert check.run_version("git") is None

    def hangs(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(check.subprocess, "run", hangs)
    assert check.run_version("git") is None


def test_the_desktop_apps_versions_come_from_folder_names(tmp_path, monkeypatch):
    monkeypatch.setattr(check.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(check.sys, "platform", "darwin")
    place = tmp_path / "Library" / "Application Support" / "Claude" / "claude-code"
    assert check.desktop_claude_code() is None
    for name in ("2.1.330", "2.1.331", "2.1.99", "not-a-version", "2.1.400.tmp"):
        (place / name).mkdir(parents=True)
        (place / name / "claude").write_text("x", encoding="utf-8")
    (place / "2.1.500").mkdir()  # empty: a half-done update doesn't count
    (place / "2.1.999").write_text("a file, not a folder", encoding="utf-8")
    assert check.desktop_claude_code() == (2, 1, 331)


def test_the_windows_store_apps_versions_are_found(tmp_path, monkeypatch):
    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    place = tmp_path / "Local" / "Claude-Data" / "claude_made-up" / "claude-code" / "2.1.304"
    place.mkdir(parents=True)
    (place / "claude.exe").write_text("x", encoding="utf-8")
    assert check.desktop_claude_code() == (2, 1, 304)


def test_an_old_claude_command_doesnt_hide_a_good_desktop_app():
    found = {**ALL, "claude": "2.1.294 (Claude Code)"}
    item = by_name(check.tool_items(runner(**found), lambda: (2, 1, 330)))["Claude Code"]
    assert item.ok
    assert "desktop app" in item.detail and "claude update" in item.detail


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("2.1.330 (Claude Code)", (2, 1, 330)),
        ("2.1.295-rc.1", (2, 1, 294)),
        ("2.1.0-beta", (2, 0, 999_999)),
        ("no version", None),
    ],
)
def test_a_pre_release_reads_as_older_than_its_release(text, version):
    assert check.parse_version(text) == version


def test_on_macos_claude_codes_own_install_is_found_off_path(tmp_path, monkeypatch):
    # #159: so the desktop app's folder isn't looked in, which macOS may refuse with a notice.
    monkeypatch.setattr(check.shutil, "which", lambda tool: None)
    monkeypatch.setattr(check.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(check.sys, "platform", "darwin")
    assert check.claude_path() is None
    older = tmp_path / ".claude" / "local" / "claude"
    older.parent.mkdir(parents=True)
    older.write_text("x", encoding="utf-8")
    older.chmod(0o755)
    assert check.claude_path() == str(older)
    native = tmp_path / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_text("x", encoding="utf-8")
    native.chmod(0o755)
    assert check.claude_path() == str(native)
    monkeypatch.setattr(check.shutil, "which", lambda tool: "/made-up/claude")
    assert check.claude_path() == "/made-up/claude"  # PATH first, as before


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_elsewhere_claude_is_looked_for_on_path_only_as_before(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(check.shutil, "which", lambda tool: None)
    monkeypatch.setattr(check.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(check.sys, "platform", platform)
    native = tmp_path / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_text("x", encoding="utf-8")
    assert check.claude_path() is None


def test_a_desktop_app_folder_the_system_refuses_counts_as_no_app(tmp_path, monkeypatch):
    monkeypatch.setattr(check.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(check.sys, "platform", "darwin")
    place = tmp_path / "Library" / "Application Support" / "Claude" / "claude-code"
    (place / "2.1.331").mkdir(parents=True)

    def refused(self):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(check.Path, "iterdir", refused)
    assert check.desktop_versions() == {}


def test_the_windows_app_alias_isnt_run(monkeypatch, tmp_path):
    monkeypatch.setattr(check.Path, "home", lambda: tmp_path)  # no Claude Code of its own here
    alias = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\claude.exe"
    monkeypatch.setattr(check.shutil, "which", lambda tool: alias)
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: pytest.fail("ran the alias"))
    assert check.run_version("claude") is None


def test_a_probe_that_never_returns_is_left_behind(monkeypatch):
    monkeypatch.setattr(check, "TIMEOUT", 0.1)
    monkeypatch.setattr(check.shutil, "which", lambda tool: f"/made-up/{tool}")
    release = check.threading.Event()

    def stuck(args, **kwargs):
        release.wait(5)
        return subprocess.CompletedProcess(args, 0, stdout="2.1.330", stderr="")

    monkeypatch.setattr(check.subprocess, "run", stuck)
    try:
        assert check.run_version("claude") is None
    finally:
        release.set()


# --- the repo folder -----------------------------------------------------------------------


def make_repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".claude" / "commands").mkdir(parents=True)
    rules = {"permissions": {"deny": ["mcp__slack__slack_send_message"]}}  # a Slack write tool
    (root / ".claude" / "settings.json").write_text(json.dumps(rules), encoding="utf-8")
    (root / ".claude" / "commands" / "slack-export.md").write_text("x", encoding="utf-8")
    return root


def test_the_repo_folder_and_a_folder_inside_it_are_ok(tmp_path):
    root = make_repo(tmp_path / "clone")
    (root / "src").mkdir()
    assert check.repo_item(root).ok
    assert check.repo_item(root / "src").ok


@pytest.mark.parametrize(
    "rules",
    [
        "{}",
        '{"permissions": {"deny": []}}',
        '{"permissions": {"deny": ["mcp__slack__slack_read_channel"]}}',  # no write rule
        "not json",
    ],
)
def test_a_clone_with_its_rules_emptied_is_not_ok(tmp_path, rules):
    root = make_repo(tmp_path / "clone")
    (root / ".claude" / "settings.json").write_text(rules, encoding="utf-8")
    assert not check.repo_item(root).ok


@POSIX
def test_rules_that_are_a_link_are_never_read(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "clone")
    rules = root / ".claude" / "settings.json"
    target = tmp_path / "team-a.diffdata-team.json"
    target.write_text(rules.read_text(encoding="utf-8"), encoding="utf-8")
    rules.unlink()
    rules.symlink_to(target)
    no_opening(monkeypatch, target.name)
    assert not check.repo_item(root).ok


def test_the_repos_real_rules_pass():
    assert check.has_rules(Path(__file__).parent.parent / ".claude" / "settings.json")


@pytest.mark.parametrize("missing", [".git", ".claude/settings.json", ".claude/commands"])
def test_another_folder_or_a_clone_without_the_rules_is_not(tmp_path, missing):
    root = make_repo(tmp_path / "clone")
    target = root / missing
    if target.is_dir():
        for path in sorted(target.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        target.rmdir()
    else:
        target.unlink()
    item = check.repo_item(root)
    assert not item.ok
    assert item.detail == REPO_FIX


# --- config.toml and the store key -------------------------------------------------------


def settings_in(tmp_path: Path) -> Path:
    folder = tmp_path / "fakeperson" / ".diffdata"
    folder.mkdir(parents=True)
    return folder / "config.toml"


def write_key(settings: Path, mode: int = 0o600) -> Path:
    key = settings.parent / check.KEY_NAME
    key.write_text(json.dumps({"private_key": MARKER}), encoding="utf-8")
    os.chmod(key, mode)
    return key


def test_the_key_name_is_the_pushs():
    assert check.KEY_NAME == push.KEY_NAME


def test_no_config_and_no_key(tmp_path):
    items = by_name(check.state_items(settings_in(tmp_path)))
    assert not items["config.toml"].ok
    assert "diffdata setup" in items["config.toml"].detail
    assert not items["Store key"].ok
    assert "team file" in items["Store key"].detail


def test_a_config_that_fails_its_checks(tmp_path):
    settings = settings_in(tmp_path)
    settings.write_text("this isn't toml [", encoding="utf-8")
    item = by_name(check.state_items(settings))["config.toml"]
    assert not item.ok
    assert "doesn't pass its checks" in item.detail


def no_opening(monkeypatch, *names: str):
    """Fail the test if anything opens a file with one of these names."""
    import builtins
    import io

    real_open, real_os_open = builtins.open, os.open

    def guarded(path, *args, **kwargs):
        if Path(os.fspath(path)).name in names:
            pytest.fail(f"opened {path}")
        return real_open(path, *args, **kwargs)

    def guarded_os(path, *args, **kwargs):
        if Path(os.fspath(path)).name in names:
            pytest.fail(f"opened {path}")
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(io, "open", guarded)
    monkeypatch.setattr(os, "open", guarded_os)


def test_must_refuse_2_the_key_is_never_opened(tmp_path, monkeypatch):
    settings = settings_in(tmp_path)
    write_key(settings)
    no_opening(monkeypatch, check.KEY_NAME)
    items = by_name(check.state_items(settings))
    assert items["Store key"].ok
    assert MARKER not in "\n".join(item.detail for item in items.values())


@POSIX
def test_must_refuse_2_a_config_that_links_to_the_key_isnt_opened(tmp_path, monkeypatch):
    settings = settings_in(tmp_path)
    key = write_key(settings)
    settings.symlink_to(key)
    no_opening(monkeypatch, check.KEY_NAME, "config.toml")
    item = by_name(check.state_items(settings))["config.toml"]
    assert not item.ok
    assert "is a link" in item.detail


def test_a_config_with_a_wrong_type_is_a_fix_not_a_crash(tmp_path):
    settings = settings_in(tmp_path)
    settings.write_text("[local]\ndata_dir = 42\n", encoding="utf-8")
    item = by_name(check.state_items(settings))["config.toml"]
    assert not item.ok


@POSIX
def test_a_key_nobody_can_read_says_so(tmp_path):
    settings = settings_in(tmp_path)
    key = write_key(settings, mode=0o000)
    try:
        item = by_name(check.state_items(settings))["Store key"]
    finally:
        os.chmod(key, 0o600)
    if os.geteuid() != 0:  # root can read anything
        assert not item.ok
        assert "can't read" in item.detail


def test_a_key_inside_a_repository_is_not_ok(tmp_path):
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    folder = clone / ".diffdata"
    folder.mkdir()
    write_key(folder / "config.toml")
    item = by_name(check.state_items(folder / "config.toml"))["Store key"]
    assert not item.ok
    assert "git repository" in item.detail


def test_claude_codes_own_line_is_read_when_a_wrapper_prints_more():
    output = "Node.js v24.1.0\n2.1.294 (Claude Code)"
    item = by_name(check.tool_items(runner(**{**ALL, "claude": output}), no_app))["Claude Code"]
    assert not item.ok
    assert "2.1.294" in item.detail


def test_the_windows_store_apps_local_cache_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    package = tmp_path / "Local" / "Packages" / "Claude_made-up"
    place = package / "LocalCache" / "Roaming" / "Claude" / "claude-code" / "2.1.299"
    place.mkdir(parents=True)
    (place / "claude.exe").write_text("x", encoding="utf-8")
    assert check.desktop_claude_code() == (2, 1, 299)


def test_the_gaps_show_when_setup_stops_with_an_os_error(monkeypatch, capsys):
    setup_cli(monkeypatch, lambda: [Item("Git", False, GIT_FIX)])

    def fails(team_file):
        raise PermissionError("made-up")

    monkeypatch.setattr(cli.producer_setup, "setup", fails)
    assert cli.main(["setup"]) == 1
    assert GIT_FIX in capsys.readouterr().out


def test_a_key_with_a_second_hard_link_is_not_ok(tmp_path):
    settings = settings_in(tmp_path)
    key = write_key(settings)
    os.link(key, tmp_path / "copy.json")
    item = by_name(check.state_items(settings))["Store key"]
    assert not item.ok
    assert "hard link" in item.detail


@POSIX
def test_a_key_others_can_read_says_chmod(tmp_path):
    settings = settings_in(tmp_path)
    write_key(settings, mode=0o644)
    item = by_name(check.state_items(settings))["Store key"]
    assert not item.ok
    assert "chmod 600" in item.detail


@POSIX
def test_a_key_that_is_a_link_is_not_ok(tmp_path):
    settings = settings_in(tmp_path)
    real = tmp_path / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    (settings.parent / check.KEY_NAME).symlink_to(real)
    item = by_name(check.state_items(settings))["Store key"]
    assert not item.ok
    assert "link or a folder" in item.detail


@POSIX
def test_must_refuse_1_check_changes_nothing(tmp_path, monkeypatch, capsys):
    settings = settings_in(tmp_path)
    key = write_key(settings, mode=0o644)
    root = make_repo(tmp_path / "clone")
    monkeypatch.setattr(check.config_module, "user_config_file", lambda: settings)

    def snapshot():
        return {
            p: (p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in tmp_path.rglob("*")
        }

    before = snapshot()
    code = check.check(runner(**ALL), no_app, root)
    after = snapshot()
    assert code == 1
    assert before == after
    assert oct(key.stat().st_mode & 0o777) == "0o644"
    assert MARKER not in capsys.readouterr().out


# --- the report, the command and setup ----------------------------------------------------


def test_check_says_ok_or_fix_and_what_it_cant_see(tmp_path, monkeypatch, capsys):
    settings = settings_in(tmp_path)
    monkeypatch.setattr(check.config_module, "user_config_file", lambda: settings)
    code = check.check(runner(**ALL), no_app, make_repo(tmp_path / "clone"))
    out = capsys.readouterr().out
    assert code == 1
    assert "  OK   Git" in out
    assert "  FIX  config.toml" in out
    assert "2 to fix" in out
    assert "Slack connector" in out


def test_before_export_lists_only_the_gaps():
    assert check.before_export([Item("Git", True, "x")]) == []
    shown = check.before_export([Item("Git", True, "x"), Item("Claude Code", False, "install")])
    assert shown[1] == "Before you export:"
    assert len(shown) == 3
    assert "FIX  Claude Code  install" in shown[2]


def test_diffdata_check_is_a_command(monkeypatch):
    monkeypatch.setattr(cli.producer_check, "check", lambda: 0)
    assert cli.main(["check"]) == 0


def setup_cli(monkeypatch, tool_items):
    ran = []
    monkeypatch.setattr(cli.review, "in_terminal", lambda stdin, stdout: True)
    monkeypatch.setattr(cli.producer_check, "tool_items", tool_items)
    monkeypatch.setattr(cli.producer_check, "repo_item", lambda: Item("Repo folder", True, "x"))
    monkeypatch.setattr(cli.producer_setup, "setup", lambda team_file: ran.append(1) or 0)
    return ran


def test_must_refuse_3_setup_runs_with_a_tool_missing_and_lists_it_last(monkeypatch, capsys):
    ran = setup_cli(monkeypatch, lambda: [Item("Claude Code", False, CLAUDE_MISSING)])
    assert cli.main(["setup"]) == 0
    assert ran == [1]
    out = capsys.readouterr().out
    assert "Before you export:" in out
    assert CLAUDE_MISSING in out


def test_setup_says_nothing_extra_when_all_is_there(monkeypatch, capsys):
    setup_cli(monkeypatch, lambda: [Item("Git", True, "x")])
    assert cli.main(["setup"]) == 0
    assert "Before you export" not in capsys.readouterr().out


def test_must_refuse_3_a_check_that_breaks_never_stops_setup(monkeypatch, capsys):
    def broken():
        raise RuntimeError("made-up failure")

    ran = setup_cli(monkeypatch, broken)
    assert cli.main(["setup"]) == 0
    assert ran == [1]


def test_the_gaps_show_when_setup_stops_with_an_error(monkeypatch, capsys):
    setup_cli(monkeypatch, lambda: [Item("Git", False, GIT_FIX)])

    def refuses(team_file):
        raise cli.producer_setup.SetupError("made-up refusal")

    monkeypatch.setattr(cli.producer_setup, "setup", refuses)
    assert cli.main(["setup"]) == 1
    assert GIT_FIX in capsys.readouterr().out


def test_a_config_with_a_second_hard_link_isnt_opened(tmp_path, monkeypatch):
    settings = settings_in(tmp_path)
    settings.write_text("[sources]\n", encoding="utf-8")
    os.link(settings, tmp_path / "elsewhere.toml")
    no_opening(monkeypatch, "config.toml", "elsewhere.toml")
    item = by_name(check.state_items(settings))["config.toml"]
    assert not item.ok
    assert "hard link" in item.detail


def test_a_broken_folder_check_keeps_the_tool_gaps(monkeypatch, capsys):
    setup_cli(monkeypatch, lambda: [Item("Git", False, GIT_FIX)])

    def broken():
        raise PermissionError("made-up")

    monkeypatch.setattr(cli.producer_check, "repo_item", broken)
    assert cli.main(["setup"]) == 0
    assert GIT_FIX in capsys.readouterr().out


# --- notices (plan #130, revision 6) -------------------------------------------------------

NOTICE = "https://example.slack.com/archives/C00000002/p1700000000000100"


def notices_config(tmp_path: Path, notices: str) -> Path:
    settings = settings_in(tmp_path)
    settings.write_text(
        "[sources]\n"
        'slack_channels = ["example-channel", "C00000002", "other-channel"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f"slack_notices = {{ {notices} }}\n"
        "[local]\n"
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return settings


def test_a_listed_channel_with_no_notice_is_a_fix_line(tmp_path):
    settings = notices_config(tmp_path, f'"example-channel" = "none", C00000002 = "{NOTICE}"')
    items = check.state_items(settings)
    fixes = [item for item in items if item.name == "Notice"]
    assert len(fixes) == 1
    assert not fixes[0].ok
    assert fixes[0].detail.startswith("Channel 3 in slack_channels (other-channel) has no notice")
    assert "uv run diffdata setup" in fixes[0].detail
    assert "answer y to Change it" in fixes[0].detail
    assert NOTICE not in fixes[0].detail
    shown = "\n".join(check.lines(items))
    assert "  FIX  Notice" in shown


def test_a_channel_listed_by_id_is_matched_by_a_link_key(tmp_path):
    link_key = '"https://example.slack.com/archives/C00000002"'
    settings = notices_config(
        tmp_path, f'"example-channel" = "none", {link_key} = "{NOTICE}", other-channel = "none"'
    )
    items = by_name(check.state_items(settings))
    assert "Notice" not in items
    assert items["Notices"].ok


def test_a_channel_listed_by_id_with_no_notice_says_so(tmp_path):
    settings = notices_config(tmp_path, '"example-channel" = "none", other-channel = "none"')
    fixes = [item for item in check.state_items(settings) if item.name == "Notice"]
    assert [f.detail.split(" has")[0] for f in fixes] == [
        "Channel 2 in slack_channels (C00000002, a channel ID)"
    ]


def test_an_empty_key_never_counts_as_a_channels_notice(tmp_path):
    settings = notices_config(tmp_path, '"" = "none"')
    fixes = [item for item in check.state_items(settings) if item.name == "Notice"]
    assert len(fixes) == 3


def test_an_unlisted_id_key_may_be_a_named_channels_and_check_says_so(tmp_path):
    settings = notices_config(
        tmp_path, f'C00000002 = "{NOTICE}", C00000009 = "none", other-channel = "none"'
    )
    fixes = [item for item in check.state_items(settings) if item.name == "Notice"]
    assert len(fixes) == 1
    assert "(An entry keyed by a channel's ID may be this one's" in fixes[0].detail
