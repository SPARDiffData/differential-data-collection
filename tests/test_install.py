"""The install scripts (#150, item 8). One test or more per must-refuse item 11 to 15, and for
#128's must-refuse 7: the team file found in Downloads only when it's certain.

install.sh runs in a fake home, with stand-ins for git, uv, curl, id, uname and xcode-select
on PATH: nothing is downloaded, cloned or installed, and the real home is never touched. The
hand-over to setup is checked with and without a terminal (a pseudo-terminal made here).
install.ps1 runs on Windows, in Windows PowerShell 5.1 and PowerShell 7, piped to
Invoke-Expression as the real line does, against the real git, for the refusals that stop
before anything is downloaded. GitHub's Windows runners are elevated, so there it shows the
administrator refusal (must-refuse 11); in a normal window it reaches the folder checks.
"""

import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
REPO_URL = "https://github.com/SPARDiffData/differential-data-collection.git"
FORK = "https://github.com/someone/fork.git"
DEST = "differential-data-collection"
MARKER = "MADE-UP-TEAM-FILE-MARKER"
FILE_PART = "\n--- the output sent to a file ---\n"  # run_in_pty puts it between the two parts
POSIX = pytest.mark.skipif(os.name == "nt", reason="install.sh is for macOS and Linux")
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows")

GIT_STUB = r"""#!/bin/sh
echo "git $* [global=${GIT_CONFIG_GLOBAL-} nosystem=${GIT_CONFIG_NOSYSTEM-}]" >> "$STUB_LOG"
[ "$1" = "--no-optional-locks" ] && shift
if [ "$1" = "--version" ]; then echo "git version 2.0 (stand-in)"; exit 0; fi
if [ "$1" = "clone" ]; then /bin/mkdir -p "$5/.git"; exit "${STUB_CLONE_EXIT:-0}"; fi
if [ "$1" = "-C" ]; then
  dest="$2"; shift 2
  case "$1 $2" in
    "rev-parse --show-toplevel") /bin/cat "$dest/.git/top" 2>/dev/null || echo "$dest"; exit 0 ;;
    "rev-parse --absolute-git-dir") /bin/cat "$dest/.git/gitdir" 2>/dev/null \
      || echo "$dest/.git"; exit 0 ;;
    "rev-parse --git-common-dir") echo ".git"; exit 0 ;;
    "remote get-url") /bin/cat "$dest/.git/remote" 2>/dev/null || exit 2; exit 0 ;;
    "rev-parse --abbrev-ref") /bin/cat "$dest/.git/branch" 2>/dev/null || echo main; exit 0 ;;
    "status --porcelain") /bin/cat "$dest/.git/status" 2>/dev/null; exit "${STUB_STATUS_EXIT:-0}" ;;
    "fetch origin") exit "${STUB_FETCH_EXIT:-0}" ;;
    "merge --ff-only") exit 0 ;;
  esac
fi
exit 0
"""
UV_STUB = r"""#!/bin/sh
echo "uv $* (in $(/bin/pwd))" >> "$STUB_LOG"
[ -t 0 ] && echo "uv stdin is a terminal" >> "$STUB_LOG"
if [ "$1" = "sync" ]; then exit "${STUB_SYNC_EXIT:-0}"; fi
exit 0
"""
LOGGER = r"""#!/bin/sh
echo "{name} $*" >> "$STUB_LOG"
{body}
"""


def stub(folder: Path, name: str, text: str) -> None:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    """A fake home, and a PATH of stand-ins only (plus sh, which the uv install pipes into)."""
    home = tmp_path / "home"
    (home / ".diffdata").mkdir(parents=True)
    (home / ".diffdata" / "config.toml").write_text("# made up\n", encoding="utf-8")
    team = home / "Downloads" / "DD-TEST.diffdata-team.json"
    team.parent.mkdir()
    team.write_text(json.dumps({"store_key": MARKER}), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "git", GIT_STUB)
    stub(bin_dir, "uv", UV_STUB)
    stub(bin_dir, "id", LOGGER.format(name="id", body='echo "${STUB_UID:-501}"'))
    stub(bin_dir, "uname", LOGGER.format(name="uname", body='echo "${STUB_OS:-Linux}"'))
    stub(bin_dir, "xcode-select", LOGGER.format(name="xcode-select", body="exit 0"))
    stub(bin_dir, "curl", LOGGER.format(name="curl", body="exit 0"))
    for tool in ("sh", "tr", "cat", "find"):
        os.symlink(shutil.which(tool), bin_dir / tool)
    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    env = {"HOME": str(home), "PATH": str(bin_dir), "STUB_LOG": str(log), "LANG": "C"}
    return {"home": home, "team": team, "bin": bin_dir, "log": log, "env": env, "tmp": tmp_path}


def run_sh(box, *args: str, tty=False, cwd=None, typed: bytes = b"", **env: str):
    """Run install.sh as `curl … | bash -s -- args` does: piped in, with no terminal unless
    `tty`. tty="in" gives it a terminal, but sends its output to a file. `typed` is what the
    person types on the terminal."""
    full = {**box["env"], **env}
    command = ["/bin/bash", "-s", "--", *args]
    script = INSTALL_SH.read_bytes()
    if not tty:
        done = subprocess.run(
            command, input=script, capture_output=True, env=full, start_new_session=True,
            timeout=60, check=False, cwd=cwd,
        )  # fmt: skip
        return done.returncode, done.stdout.decode() + done.stderr.decode()
    return run_in_pty(command, script, full, output_to_file=(tty == "in"), cwd=cwd, typed=typed)


def run_in_pty(
    command, script: bytes, env, output_to_file=False, cwd=None, typed: bytes = b""
) -> tuple[int, str]:
    """Run with a pseudo-terminal as the controlling terminal, so /dev/tty opens."""
    import fcntl
    import pty
    import tempfile
    import termios

    master, slave = pty.openpty()

    def own_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    sink = tempfile.TemporaryFile() if output_to_file else None
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=sink or slave, stderr=sink or slave, env=env,
        preexec_fn=own_terminal, close_fds=True, cwd=cwd,
    )  # fmt: skip
    if typed:
        # Held by the terminal until the script reads it. The terminal is kept open here until
        # the script ends: when nobody has it open, the system throws away what was typed.
        os.write(master, typed)
    else:
        os.close(slave)
    child.stdin.write(script)
    child.stdin.close()
    output = b""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            if child.poll() is not None:
                break
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
    code = child.wait(timeout=60)
    if typed:
        os.close(slave)
    os.close(master)
    if sink is not None:
        sink.seek(0)
        output += FILE_PART.encode() + sink.read()
    return code, output.decode(errors="replace")


def calls(box) -> list[str]:
    return box["log"].read_text(encoding="utf-8").splitlines()


def clone(box, remote=REPO_URL, branch="main", status="", top=None, gitdir=None) -> Path:
    dest = box["home"] / DEST
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "remote").write_text(remote + "\n", encoding="utf-8")
    (dest / ".git" / "branch").write_text(branch + "\n", encoding="utf-8")
    (dest / ".git" / "status").write_text(status, encoding="utf-8")
    if top:
        (dest / ".git" / "top").write_text(top + "\n", encoding="utf-8")
    if gitdir:
        (dest / ".git" / "gitdir").write_text(gitdir + "\n", encoding="utf-8")
    return dest


def changed_nothing(box) -> None:
    for call in calls(box):
        assert not call.startswith(("git clone", "curl", "uv ")), call
        assert " fetch " not in call and " merge " not in call, call


# --- a whole install ----------------------------------------------------------------------


@POSIX
def test_a_fresh_install_clones_syncs_and_prints_the_setup_line(box):
    code, out = run_sh(box, str(box["team"]))
    assert code == 0, out
    dest = box["home"] / DEST
    assert f"git clone --branch main {REPO_URL} {dest} [global=/dev/null nosystem=1]" in calls(box)
    assert f"uv sync --locked --project {dest} (in {dest})" in calls(box)
    assert "Now run setup yourself" in out
    assert f"run diffdata setup {box['team']}" in out
    assert not any("diffdata setup" in c for c in calls(box))  # no terminal: not run


@POSIX
def test_with_a_terminal_it_hands_over_to_setup_on_the_terminal(box):
    code, out = run_sh(box, str(box["team"]), tty=True)
    assert code == 0, out
    dest = box["home"] / DEST
    log = calls(box)
    setup = log.index(f"uv run diffdata setup {box['team']} (in {dest})")
    assert log[setup + 1] == "uv stdin is a terminal"  # setup reads the person, not the pipe
    assert "uv stdin is a terminal" not in log[:setup]  # nothing before it reads the terminal


@POSIX
def test_with_a_terminal_and_no_path_setup_gets_the_file_from_downloads(box):
    code, out = run_sh(box, tty=True)
    assert code == 0, out
    assert any(c.startswith(f"uv run diffdata setup {box['team']} (in") for c in calls(box))


@POSIX
def test_a_clean_clone_is_fetched_then_fast_forwarded(box):
    dest = clone(box)
    code, out = run_sh(box)
    assert code == 0, out
    assert f"git -C {dest} fetch origin main [global=/dev/null nosystem=1]" in calls(box)
    merge = f"git -C {dest} merge --ff-only --no-overwrite-ignore FETCH_HEAD [global=/dev/null"
    assert any(c.startswith(merge) for c in calls(box))
    status = f"git --no-optional-locks -C {dest} status --porcelain --untracked-files=normal"
    assert any(c.startswith(status + " [global=/dev/null nosystem=1]") for c in calls(box))
    assert not any(c.startswith("git clone") or " pull " in c for c in calls(box))


@POSIX
def test_a_missing_uv_is_installed_from_astral_only(box):
    (box["bin"] / "uv").rename(box["tmp"] / "uv-impl")
    stub(
        box["bin"], "curl",
        LOGGER.format(
            name="curl",
            body=f'echo \'/bin/mkdir -p "$HOME/.local/bin"; /bin/cp "{box["tmp"]}/uv-impl" '
            '"$HOME/.local/bin/uv"; echo "installer UV_DOWNLOAD_URL=${UV_DOWNLOAD_URL-unset}" '
            '>> "$STUB_LOG"\'',
        ),
    )  # fmt: skip
    code, out = run_sh(box, UV_DOWNLOAD_URL="https://elsewhere.example/uv")
    assert code == 0, out
    assert "curl -q -LsSf https://astral.sh/uv/install.sh" in calls(box)
    assert "installer UV_DOWNLOAD_URL=unset" in calls(box)  # overrides cleared (15)
    assert any(c.startswith("uv sync") for c in calls(box))


@POSIX
def test_a_relative_team_file_is_passed_as_an_absolute_path(box):
    code, out = run_sh(box, "Downloads/DD-TEST.diffdata-team.json", tty=True, cwd=box["home"])
    assert code == 0, out
    assert any(f"run diffdata setup {box['team']} " in c for c in calls(box))


@POSIX
def test_the_printed_line_quotes_odd_paths_for_the_shell(box):
    odd = box["tmp"] / "a $HOME `x` folder"
    code, out = run_sh(box, DIFFDATA_REPO=str(odd))
    assert code == 0, out
    line = next(x for x in out.splitlines() if "run diffdata setup" in x).strip()
    quoted_dest = line.split(" && ")[0].removeprefix("cd ")
    done = subprocess.run(["/bin/bash", "-c", f"printf '%s' {quoted_dest}"],
                          capture_output=True, text=True, check=True)  # fmt: skip
    assert done.stdout == str(odd)


# --- must refuse 11: never as root -------------------------------------------------------


@POSIX
def test_must_refuse_11_sudo_to_another_user_stops_too(box):
    code, out = run_sh(box, SUDO_USER="someone")
    assert code == 1 and "Don't run this as root" in out
    assert calls(box) == ["id -u"]


@POSIX
@pytest.mark.parametrize("path", ["{tmp}/a/../b", "{tmp}/./b"])
def test_must_refuse_12_dots_in_the_folder_stop(box, path):
    code, out = run_sh(box, DIFFDATA_REPO=path.format(tmp=box["tmp"]))
    assert code == 1 and "without . or .." in out
    changed_nothing(box)


@POSIX
def test_must_refuse_12_diffdata_spelt_in_other_capitals_stops(box):
    code, out = run_sh(box, DIFFDATA_REPO=str(box["home"] / ".DIFFDATA" / "repo"))
    assert code == 1 and "inside ~/.diffdata" in out
    changed_nothing(box)


@POSIX
def test_must_refuse_12_uv_project_overrides_are_cleared(box):
    record = 'echo "UV_PROJECT=${UV_PROJECT-unset}" >> "$STUB_LOG"\n'
    stub(box["bin"], "uv", UV_STUB.replace('echo "uv $*', record + 'echo "uv $*', 1))
    code, out = run_sh(box, UV_PROJECT="/somewhere/else", UV_WORKING_DIR="/somewhere/else")
    assert code == 0, out
    assert "UV_PROJECT=unset" in calls(box)


@POSIX
def test_must_refuse_11_root_stops_before_anything(box):
    code, out = run_sh(box, STUB_UID="0")
    assert code == 1
    assert "Don't run this as root" in out
    assert calls(box) == ["id -u"]


# --- must refuse 12: only a clean clone of this repo on main ------------------------------


@POSIX
@pytest.mark.parametrize(
    "make, words",
    [
        (lambda box: (box["home"] / DEST).mkdir(), "isn't a clone"),
        (lambda box: (box["home"] / DEST).write_text("x"), "isn't a clone"),
        (lambda box: clone(box, remote=FORK), "another repository"),
        (lambda box: clone(box, branch="my-work"), "not main"),
        (lambda box: clone(box, branch="MAIN"), "not main"),
        (lambda box: clone(box, status=" M README.md\n"), "local changes"),
        (lambda box: clone(box, status="?? my-notes.md\n"), "local changes"),
        (lambda box: clone(box, top="/somewhere/else"), "point to another folder"),
        (lambda box: clone(box, gitdir=str(box["tmp"])), "point to another folder"),
    ],
)  # fmt: skip
def test_must_refuse_12_anything_but_a_clean_clone_stops(box, make, words):
    make(box)
    code, out = run_sh(box)
    assert code == 1
    assert words in out and "Nothing was changed" in out
    changed_nothing(box)


@POSIX
def test_must_refuse_12_a_failed_status_check_stops(box):
    clone(box)
    code, out = run_sh(box, STUB_STATUS_EXIT="128")
    assert code == 1 and "Couldn't check" in out
    changed_nothing(box)


@POSIX
@pytest.mark.parametrize("slash", ["", "/"])
def test_must_refuse_12_a_link_in_the_way_stops(box, slash):
    elsewhere = box["tmp"] / "elsewhere"
    (elsewhere / ".git").mkdir(parents=True)
    os.symlink(elsewhere, box["home"] / DEST)
    code, out = run_sh(box, DIFFDATA_REPO=str(box["home"] / DEST) + slash)
    assert code == 1
    assert "isn't a clone" in out
    changed_nothing(box)


@POSIX
@pytest.mark.parametrize("inside", ["repo", "new/deeper/repo"])
def test_must_refuse_12_a_folder_inside_diffdata_stops(box, inside):
    code, out = run_sh(box, DIFFDATA_REPO=str(box["home"] / ".diffdata" / inside))
    assert code == 1 and "inside ~/.diffdata" in out
    changed_nothing(box)
    assert sorted(p.name for p in (box["home"] / ".diffdata").iterdir()) == ["config.toml"]


@POSIX
def test_must_refuse_12_diffdata_through_a_link_stops(box):
    os.symlink(box["home"] / ".diffdata", box["tmp"] / "alias")
    code, out = run_sh(box, DIFFDATA_REPO=str(box["tmp"] / "alias" / "new" / "repo"))
    assert code == 1 and "inside ~/.diffdata" in out
    changed_nothing(box)


@POSIX
def test_must_refuse_12_the_folder_is_checked_before_anything_is_installed(box):
    (box["bin"] / "uv").unlink()
    (box["home"] / DEST).mkdir()
    code, out = run_sh(box)
    assert code == 1
    changed_nothing(box)  # no curl: uv wasn't installed first


@POSIX
def test_must_refuse_12_git_settings_from_the_environment_are_cleared(box):
    stub(box["bin"], "git", GIT_STUB.replace(
        'echo "git $*', 'echo "GIT_DIR=${GIT_DIR-unset} GIT_WORK_TREE=${GIT_WORK_TREE-unset}" '
        '>> "$STUB_LOG"\necho "git $*', 1))  # fmt: skip
    code, out = run_sh(box, GIT_DIR="/somewhere/else/.git", GIT_WORK_TREE="/somewhere/else")
    assert code == 0, out
    seen = [c for c in calls(box) if c.startswith("GIT_DIR=")]
    assert seen and all(c == "GIT_DIR=unset GIT_WORK_TREE=unset" for c in seen)


# --- must refuse 13: the team file is only passed on ----------------------------------------


@POSIX
def test_must_refuse_13_the_team_file_and_diffdata_folder_are_untouched(box):
    before = {p: p.read_bytes() for p in (box["team"], box["home"] / ".diffdata" / "config.toml")}
    for tty in (False, True):
        dest = str(box["tmp"] / f"repo-{tty}")
        code, out = run_sh(box, str(box["team"]), tty=tty, DIFFDATA_REPO=dest)
        assert code == 0, out
        assert MARKER not in out
    for path, data in before.items():
        assert path.read_bytes() == data
    assert sorted(p.name for p in (box["home"] / ".diffdata").iterdir()) == ["config.toml"]


@POSIX
def test_must_refuse_13_the_team_file_is_never_opened(box):
    # A named pipe blocks whoever opens it: if the script read the team file, it would hang.
    pipe = box["tmp"] / "DD-PIPE.diffdata-team.json"
    os.mkfifo(pipe)
    code, out = run_sh(box, str(pipe))
    assert code == 0, out
    assert f"run diffdata setup {pipe}" in out


# --- must refuse 14: no going on after a failure; setup only for a person -------------------


@POSIX
def test_must_refuse_14_a_failed_step_stops_before_setup(box):
    code, out = run_sh(box, str(box["team"]), tty=True, STUB_SYNC_EXIT="3")
    assert code != 0
    assert "Installed" not in out
    assert not any("diffdata setup" in c for c in calls(box))


@POSIX
def test_must_refuse_14_a_failed_clone_stops(box):
    code, out = run_sh(box, STUB_CLONE_EXIT="128")
    assert code != 0
    assert not any(c.startswith("uv ") for c in calls(box))


@POSIX
def test_must_refuse_14_a_failed_fetch_stops(box):
    clone(box)
    code, out = run_sh(box, STUB_FETCH_EXIT="1")
    assert code != 0
    assert not any(" merge " in c or c.startswith("uv ") for c in calls(box))


@POSIX
@pytest.mark.parametrize("value", ["1", ""])
def test_must_refuse_14_claude_code_gets_the_line_not_setup(box, value):
    code, out = run_sh(box, str(box["team"]), tty=True, CLAUDECODE=value)
    assert code == 0, out
    assert "Now run setup yourself" in out
    assert not any("diffdata setup" in c for c in calls(box))


@POSIX
def test_must_refuse_14_output_not_to_a_terminal_gets_the_line(box):
    code, out = run_sh(box, str(box["team"]), tty="in")
    assert code == 0, out
    assert "Now run setup yourself" in out
    assert not any("diffdata setup" in c for c in calls(box))


@POSIX
def test_missing_git_on_macos_starts_apples_installer_and_stops(box):
    (box["bin"] / "git").unlink()
    code, out = run_sh(box, STUB_OS="Darwin")
    assert code == 1
    assert "xcode-select --install" in calls(box)
    assert "run the install line again" in out
    assert not any(c.startswith(("uv ", "curl")) for c in calls(box))


@POSIX
def test_missing_git_on_linux_stops_with_no_sudo(box):
    (box["bin"] / "git").unlink()
    code, out = run_sh(box)
    assert code == 1
    assert "Git is missing" in out and "sudo" not in out
    assert not any(c.startswith(("xcode-select", "uv ", "curl")) for c in calls(box))


# --- #128, must refuse 7: the team file is found in Downloads, only when it's certain -----


def team_name(code: str) -> str:
    return f"{code}.diffdata-team.json"


def team_file(folder: Path, code: str) -> Path:
    """A made-up team file: a project code's name, with the marker as its contents."""
    path = folder / f"{code}.diffdata-team.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"store_key": MARKER}), encoding="utf-8")
    return path


def no_team_file(box) -> None:
    box["team"].unlink()


@POSIX
def test_one_team_file_in_downloads_is_used(box):
    code, out = run_sh(box)
    assert code == 0, out
    assert f"Your team file: {box['team']}" in out
    assert f"run diffdata setup {box['team']}" in out
    assert MARKER not in out


@POSIX
def test_two_team_files_ask_for_the_project_code(box):
    other = team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box, tty=True, typed=b"7kq4\n")
    assert code == 0, out
    assert "There are 2 team files in Downloads: DD-7KQ4 DD-TEST." in out
    assert any(c.startswith(f"uv run diffdata setup {other} (in") for c in calls(box))
    assert MARKER not in out


@POSIX
def test_two_team_files_ask_on_the_terminal_even_when_output_goes_to_a_file(box):
    other = team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box, tty="in", typed=b"7KQ4\n")
    assert code == 0, out
    on_terminal = out.split(FILE_PART)[0]
    assert "Which is your project code?" in on_terminal and "DD-7KQ4 DD-TEST" in on_terminal
    assert f"Your team file: {other}" in out


@POSIX
def test_two_team_files_take_the_code_with_dd_in_front(box):
    team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box, tty=True, typed=b" DD-TEST \n")
    assert code == 0, out
    assert any(c.startswith(f"uv run diffdata setup {box['team']} (in") for c in calls(box))


@POSIX
@pytest.mark.parametrize("typed", [b"DD-XXXX\n", b"\n", b"../DD-TEST\n", b"DD-TES*\n"])
def test_two_team_files_and_a_wrong_code_stop(box, typed):
    team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box, tty=True, typed=typed)
    assert code == 1, out
    assert "isn't one of them" in out and "Nothing was changed" in out
    changed_nothing(box)


@POSIX
def test_two_team_files_without_a_terminal_stop_and_ask_for_the_path(box):
    team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box)
    assert code == 1, out
    assert "There are 2 team files" in out and "bash -s --" in out
    changed_nothing(box)


@POSIX
def test_two_team_files_under_claude_code_stop(box):
    team_file(box["home"] / "Downloads", "DD-7KQ4")
    code, out = run_sh(box, tty=True, typed=b"7KQ4\n", CLAUDECODE="1")
    assert code == 1, out
    assert "There are 2 team files" in out
    changed_nothing(box)


@POSIX
def test_no_team_file_stops_before_anything(box):
    no_team_file(box)
    code, out = run_sh(box, tty=True)
    assert code == 1
    assert "No team file in" in out and "project team's message" in out
    assert "run the install line again" in out
    changed_nothing(box)
    assert not any(c.startswith("git") for c in calls(box))


@POSIX
def test_no_team_file_but_a_second_download_names_it(box):
    box["team"].rename(box["team"].with_name("DD-TEST.diffdata-team (1).json"))
    code, out = run_sh(box)
    assert code == 1
    assert "DD-TEST.diffdata-team (1).json doesn't count" in out
    changed_nothing(box)


@POSIX
def test_no_team_file_on_a_mac_mentions_terminals_access_to_downloads(box):
    no_team_file(box)
    code, out = run_sh(box, STUB_OS="Darwin")
    assert code == 1 and "Files and Folders" in out
    code, out = run_sh(box)  # Linux: no such setting
    assert code == 1 and "Files and Folders" not in out


@POSIX
def test_no_downloads_folder_at_all_stops(box):
    shutil.rmtree(box["home"] / "Downloads")
    code, out = run_sh(box)
    assert code == 1 and "No team file in" in out
    changed_nothing(box)


@POSIX
@pytest.mark.parametrize(
    "make",
    [
        # A link named like a team file, to a real one somewhere else.
        lambda box, d: os.symlink(team_file(box["tmp"] / "x", "DD-7KQ4"), d / team_name("DD-7KQ4")),
        # A folder named like one.
        lambda box, d: (d / "DD-7KQ4.diffdata-team.json").mkdir(),
        # A named pipe named like one: never opened, so this would hang if it were.
        lambda box, d: os.mkfifo(d / "DD-7KQ4.diffdata-team.json"),
        # One in a subfolder of Downloads.
        lambda box, d: team_file(d / "older", "DD-7KQ4"),
        # Names that aren't a project code.
        lambda box, d: team_file(d, "DD-7KQ0"),
        lambda box, d: team_file(d, "dd-7kq4"),
        lambda box, d: team_file(d, "DD-7KQ44"),
        lambda box, d: (d / "DD-7KQ4.diffdata-team (1).json").write_text("x"),
        lambda box, d: (d / "DD-7KQ4.diffdata-team.json.txt").write_text("x"),
        # A hard link: a second name for a file somewhere else.
        lambda box, d: os.link(team_file(box["tmp"] / "x", "DD-7KQ4"), d / team_name("DD-7KQ4")),
    ],
    ids=["link", "folder", "pipe", "subfolder", "zero", "lowercase", "five", "copy", "txt", "hard"],
)  # fmt: skip
def test_anything_but_a_team_file_at_the_top_of_downloads_is_ignored(box, make):
    no_team_file(box)
    make(box, box["home"] / "Downloads")
    code, out = run_sh(box)
    assert code == 1 and "No team file in" in out, out
    changed_nothing(box)


@POSIX
def test_a_link_beside_one_real_team_file_is_ignored(box):
    os.symlink(team_file(box["tmp"] / "elsewhere", "DD-7KQ4"),
               box["home"] / "Downloads" / "DD-7KQ4.diffdata-team.json")  # fmt: skip
    code, out = run_sh(box)
    assert code == 0, out
    assert f"run diffdata setup {box['team']}" in out and "There are" not in out


@POSIX
def test_the_path_given_wins_over_downloads(box):
    team_file(box["home"] / "Downloads", "DD-7KQ4")
    kept = team_file(box["tmp"] / "keys", "DD-9ABC")
    code, out = run_sh(box, str(kept), tty=True)
    assert code == 0, out
    assert "There are" not in out and "Your team file" not in out
    assert any(c.startswith(f"uv run diffdata setup {kept} (in") for c in calls(box))


@POSIX
def test_a_renamed_linux_downloads_folder_is_used(box):
    no_team_file(box)
    renamed = box["home"] / "Téléchargements"
    found = team_file(renamed, "DD-7KQ4")
    stub(box["bin"], "xdg-user-dir", f'#!/bin/sh\necho "{renamed}"\n')
    code, out = run_sh(box)
    assert code == 0, out
    assert f"Your team file: {found}" in out


# --- must refuse 15: no sudo, no other hosts ---------------------------------------------


def code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]  # fmt: skip


ALLOWED = {
    "https://github.com/SPARDiffData/differential-data-collection.git",
    "https://github.com/SPARDiffData/differential-data-collection",
    "https://astral.sh/uv/install.sh",
    "https://astral.sh/uv/install.ps1",
    "https://astral.sh/uv",  # named in a message
    "https://git-scm.com/download/linux",  # named in a message, never fetched
    "https://git-scm.com/download/win",  # named in a message, never fetched
    "https://claude.com/claude-code",  # named in a message, never fetched
}


@pytest.mark.parametrize("path", [INSTALL_SH, INSTALL_PS1], ids=["sh", "ps1"])
def test_must_refuse_15_no_other_hosts_and_no_sudo(path):
    lines = code_lines(path)
    urls = {u.rstrip(").,'\"`") for line in lines for u in re.findall(r"https?://[^\s'\"]+", line)}
    assert urls <= ALLOWED, urls - ALLOWED
    for line in lines:
        # sudo run as a command; a message may name it ("Don't run this with sudo").
        assert not re.search(r"(^|[;&|(]|\$\()\s*sudo\s", line.strip()), line
        assert not re.search(r"\.(bashrc|zshrc|profile|bash_profile)\b|\$PROFILE", line), line
    downloads = [
        line for line in lines if re.search(r"\b(curl|irm|Invoke-WebRequest|wget)\b", line)
    ]
    for line in downloads:
        assert "astral.sh/uv/install." in line, line


@pytest.mark.parametrize("path", [INSTALL_SH, INSTALL_PS1], ids=["sh", "ps1"])
def test_must_refuse_15_fetching_leaves_out_your_git_settings_and_uv_overrides(path):
    text = path.read_text(encoding="utf-8")
    assert "GIT_CONFIG_NOSYSTEM" in text and "GIT_CONFIG_GLOBAL" in text
    assert "UV_DOWNLOAD_URL" in text


def test_the_scripts_have_unix_line_endings_and_sh_is_bash():
    for path in (INSTALL_SH, INSTALL_PS1):
        assert b"\r\n" not in path.read_bytes()
    assert INSTALL_SH.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


def test_ps1_stops_with_return_never_by_ending_the_session():
    # Under `irm | iex`, `exit` would close the person's PowerShell window.
    assert not any(re.search(r"\bexit\b", line, re.IGNORECASE) for line in code_lines(INSTALL_PS1))


# --- install.ps1, on Windows ------------------------------------------------------------

SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
# Set by .github/scripts/test-as-non-admin.ps1, which runs these tests as a throwaway non-admin
# user: then a test must reach the refusal it's for, never the administrator one.
NONADMIN = os.environ.get("DIFFDATA_CI_NONADMIN") == "1"


DOWNLOADS_ID = "{374DE290-123F-4565-9164-39C4925E467B}"
SHELL_FOLDERS = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"


def run_ps1(shell: str, tmp_path, dest: Path, known=None, **env) -> tuple[int, str]:
    """Run install.ps1 as `irm … | iex` does. The lookup of Windows' Downloads folder is
    always stood in for, so the machine's own one is never looked at: `known` is the folder
    it reports (None: none recorded). DIFFDATA_TEAM_FILE is a made-up path unless given, and
    left out when given as None."""
    base = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "DIFFDATA_TEAM_FILE")}
    full = {
        **base, "DIFFDATA_REPO": str(dest), "USERPROFILE": str(tmp_path),
        "DIFFDATA_TEAM_FILE": str(tmp_path / "keys" / "DD-TEST.diffdata-team.json"), **env,
    }  # fmt: skip
    full = {k: v for k, v in full.items() if v is not None}
    value = "" if known is None else str(known).replace("'", "''")
    reported = f"[pscustomobject]@{{'{DOWNLOADS_ID}' = '{value}'}}" if value else ""
    # A function comes before the cmdlet of the same name, inside the script's block too. It
    # answers only for the Downloads folder's own key and value name.
    asked = f"($args -ccontains '{SHELL_FOLDERS}') -and ($args -ccontains '{DOWNLOADS_ID}')"
    command = (
        f"function Get-ItemProperty {{ if ({asked}) {{ {reported} }} }}; "
        f"Get-Content -Raw -LiteralPath '{INSTALL_PS1}' | Invoke-Expression"
    )
    done = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, env=full, timeout=180, check=False,
    )  # fmt: skip
    return done.returncode, done.stdout + done.stderr


def elevated(shell: str) -> bool:
    check = (
        "(New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]"
        "::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    )
    done = subprocess.run([shell, "-NoProfile", "-Command", check], capture_output=True,
                          text=True, timeout=60, check=False)  # fmt: skip
    return done.stdout.strip() == "True"


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Example", "-c", "user.email=example@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
    )  # fmt: skip


def ps1_clone(tmp_path, remote=REPO_URL, change=False, untracked=False) -> Path:
    dest = tmp_path / "repo"
    dest.mkdir()
    git("init", cwd=dest)
    (dest / "README.md").write_text("made up\n", encoding="utf-8")
    git("add", "README.md", cwd=dest)
    git("commit", "-m", "made up", cwd=dest)
    git("remote", "add", "origin", remote, cwd=dest)
    if change:
        (dest / "README.md").write_text("changed\n", encoding="utf-8")
    if untracked:
        (dest / "notes.md").write_text("mine\n", encoding="utf-8")
    return dest


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "make, words",
    [
        (lambda t: (t / "repo").mkdir() or t / "repo", "isn't a clone"),
        (lambda t: ps1_clone(t, remote=FORK), "another repository"),
        (lambda t: ps1_clone(t, change=True), "local changes"),
        (lambda t: ps1_clone(t, untracked=True), "local changes"),
    ],
)  # fmt: skip
def test_ps1_must_refuse_11_or_12(tmp_path, shell, make, words):
    dest = make(tmp_path)
    before = sorted(p.name for p in dest.iterdir())
    code, out = run_ps1(shell, tmp_path, dest)
    if NONADMIN:
        assert not elevated(shell), "the non-admin run is elevated"
    if elevated(shell):  # GitHub's Windows runners: the administrator refusal comes first
        assert "Don't run this as administrator" in out, out
    else:
        assert words in out, out
    assert "Nothing was changed" in out
    assert sorted(p.name for p in dest.iterdir()) == before
    assert "Starting setup" not in out and "Getting diffdata" not in out


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_must_refuse_12_a_folder_inside_diffdata(tmp_path, shell):
    dest = tmp_path / ".diffdata" / "repo"
    code, out = run_ps1(shell, tmp_path, dest)
    if NONADMIN:
        assert not elevated(shell) and "inside ~\\.diffdata" in out, out
    assert "administrator" in out or "inside ~\\.diffdata" in out, out
    assert not dest.exists()


# Offline runs of the whole install: stand-in git and uv (.cmd files calling this stub) come
# first on PATH, so nothing is cloned, downloaded or installed.
WIN_STUB = r"""
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
tool, rest = args[0], args[1:]
if tool == "uv":
    sys.exit(int(os.environ.get("STUB_SYNC_EXIT", "0")) if rest[:1] == ["sync"] else 0)
if rest[:1] == ["--no-optional-locks"]:
    rest = rest[1:]
if rest[:1] == ["--version"]:
    print("git version 2.45.0 (stand-in)")
elif rest[:1] == ["clone"]:
    pathlib.Path(rest[-1], ".git").mkdir(parents=True)
sys.exit(0)
"""


def stubbed_install(shell: str, tmp_path, known=None, **env) -> tuple[int, str, list[str], Path]:
    if elevated(shell):
        if NONADMIN:
            pytest.fail("the non-admin run is elevated")
        pytest.skip("elevated: the administrator refusal comes first (tested above)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "stub.py").write_text(WIN_STUB, encoding="utf-8")
    for tool in ("git", "uv"):
        (bin_dir / f"{tool}.cmd").write_text(
            f'@"{sys.executable}" "{bin_dir / "stub.py"}" {tool} %*\r\n', encoding="utf-8"
        )
    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    (tmp_path / ".diffdata").mkdir()
    (tmp_path / ".diffdata" / "config.toml").write_text("# made up\n", encoding="utf-8")
    team = team_file(tmp_path / "Downloads", "DD-TEST")  # beside any a test put there first
    dest = tmp_path / "repo"
    env.setdefault("DIFFDATA_TEAM_FILE", str(team))
    code, out = run_ps1(
        shell, tmp_path, dest, known, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log), **env,
    )  # fmt: skip
    return code, out, log.read_text(encoding="utf-8").splitlines(), team


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_must_refuse_13_a_whole_install_only_passes_the_team_file_on(tmp_path, shell):
    code, out, calls_made, team = stubbed_install(shell, tmp_path)
    assert "Getting diffdata" in out and "Installed. Now run setup yourself" in out, out
    assert str(team) in out and MARKER not in out
    assert json.loads(team.read_text(encoding="utf-8")) == {"store_key": MARKER}
    assert (tmp_path / ".diffdata" / "config.toml").read_text(encoding="utf-8") == "# made up\n"
    assert sorted(p.name for p in (tmp_path / ".diffdata").iterdir()) == ["config.toml"]
    assert any(c.startswith("uv sync --locked --project") for c in calls_made)
    assert not any("diffdata setup" in c for c in calls_made)  # no terminal: not started


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_must_refuse_14_a_failed_step_stops_before_setup(tmp_path, shell):
    code, out, calls_made, _ = stubbed_install(shell, tmp_path, STUB_SYNC_EXIT="3")
    assert "uv sync failed" in out, out
    assert "Installed" not in out
    assert not any("diffdata setup" in c for c in calls_made)


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_must_refuse_14_claude_code_gets_the_line_not_setup(tmp_path, shell):
    code, out, calls_made, _ = stubbed_install(shell, tmp_path, CLAUDECODE="1")
    assert "Now run setup yourself" in out, out
    assert not any("diffdata setup" in c for c in calls_made)


# --- install.ps1 and #128's must-refuse 7: the team file found in Downloads ---------------


def nothing_installed(out: str, calls_made: list[str]) -> None:
    assert "Nothing was changed" in out, out
    assert "Getting diffdata" not in out and "Installed" not in out
    assert not any(c.startswith(("clone", "uv")) for c in calls_made), calls_made


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_one_team_file_in_downloads_is_used(tmp_path, shell):
    code, out, calls_made, team = stubbed_install(shell, tmp_path, DIFFDATA_TEAM_FILE=None)
    assert f"Your team file: {team}" in out, out
    assert "Installed. Now run setup yourself" in out and str(team) in out
    assert MARKER not in out


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_a_moved_downloads_folder_is_used(tmp_path, shell):
    moved = tmp_path / "OneDrive" / "Downloads"
    found = team_file(moved, "DD-7KQ4")
    code, out, calls_made, home_one = stubbed_install(
        shell, tmp_path, known=moved, DIFFDATA_TEAM_FILE=None
    )
    assert f"Your team file: {found}" in out, out
    assert str(home_one) not in out  # the usual folder isn't looked at once it's moved


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_a_downloads_folder_recorded_with_userprofile_is_expanded(tmp_path, shell):
    found = team_file(tmp_path / "Moved", "DD-7KQ4")
    code, out, calls_made, _ = stubbed_install(
        shell, tmp_path, known="%USERPROFILE%\\Moved", DIFFDATA_TEAM_FILE=None
    )
    assert f"Your team file: {found}" in out, out


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_no_team_file_stops_before_anything(tmp_path, shell):
    code, out, calls_made, _ = stubbed_install(
        shell, tmp_path, known=tmp_path / "Empty", DIFFDATA_TEAM_FILE=None
    )
    assert "No team file in" in out and "project team's message" in out, out
    nothing_installed(out, calls_made)


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_two_team_files_without_a_person_stop(tmp_path, shell):
    team_file(tmp_path / "Downloads", "DD-7KQ4")
    code, out, calls_made, _ = stubbed_install(shell, tmp_path, DIFFDATA_TEAM_FILE=None)
    assert "There are 2 team files" in out and "DD-7KQ4, DD-TEST" in out, out
    assert "DIFFDATA_TEAM_FILE" in out
    nothing_installed(out, calls_made)


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
def test_ps1_the_environment_variable_wins_over_downloads(tmp_path, shell):
    team_file(tmp_path / "Downloads", "DD-7KQ4")
    kept = team_file(tmp_path / "keys", "DD-9ABC")
    code, out, calls_made, _ = stubbed_install(shell, tmp_path, DIFFDATA_TEAM_FILE=str(kept))
    assert "There are" not in out and "Your team file" not in out, out
    assert "Installed. Now run setup yourself" in out and str(kept) in out


@WINDOWS
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("make", ["folder", "subfolder", "zero", "lowercase", "copy", "link"])
def test_ps1_anything_but_a_team_file_at_the_top_of_downloads_is_ignored(tmp_path, shell, make):
    downloads = tmp_path / "Moved"
    downloads.mkdir()
    name = downloads / "DD-7KQ4.diffdata-team.json"
    if make == "folder":
        name.mkdir()
    elif make == "subfolder":
        team_file(downloads / "older", "DD-7KQ4")
    elif make == "zero":
        team_file(downloads, "DD-7KQ0")
    elif make == "lowercase":
        team_file(downloads, "dd-7kq4")
    elif make == "copy":
        (downloads / "DD-7KQ4.diffdata-team (1).json").write_text("x", encoding="utf-8")
    else:
        try:
            os.symlink(team_file(tmp_path / "elsewhere", "DD-7KQ4"), name)
        except OSError:
            pytest.skip("this user can't make links on Windows")
    code, out, calls_made, _ = stubbed_install(
        shell, tmp_path, known=downloads, DIFFDATA_TEAM_FILE=None
    )
    assert "No team file in" in out, out
    if make == "copy":
        assert "DD-7KQ4.diffdata-team (1).json doesn't count" in out, out
    nothing_installed(out, calls_made)


@pytest.mark.skipif(not SHELLS, reason="needs PowerShell")
@pytest.mark.parametrize("shell", SHELLS or ["none"])
def test_ps1_parses(shell):
    command = (
        "$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$e) | Out-Null; if ($e) {{ $e; exit 1 }}"
    )
    done = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True,
                          text=True, timeout=60, check=False)  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
