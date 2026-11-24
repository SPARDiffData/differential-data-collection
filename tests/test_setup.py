"""`diffdata setup` and `diffdata team-command` (#130). One test or more per must-refuse item.

Every test runs in a fake home: HOME and USERPROFILE point at a temporary folder, and
config.toml's place is inside it. No test reads the real ~/.diffdata/. The key file is made
up, with a marker that must never show in what setup prints. Names are examples only.
"""

import json
import os
import socket
import stat
import tomllib
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common import config as config_module
from diffdata.common import setup
from diffdata.common.setup import PSEUDONYM, Prompter, SetupError

TEAM = "bucket=example-bucket,folder=team-a,project=example-project"
LINK = "https://example.slack.com/archives/C00000002"
MARKER = "MADE-UP-KEY-MARKER"
KEY = {
    "type": "service_account",
    "private_key": f"-----BEGIN PRIVATE KEY-----\n{MARKER}\n-----END PRIVATE KEY-----\n",
    "client_email": f"{MARKER.lower()}@example-project.iam.gserviceaccount.com",
    "project_id": MARKER.lower(),
}
POSIX = pytest.mark.skipif(os.name == "nt", reason="file modes are for macOS and Linux")


class Script(Prompter):
    """Answers in order, and everything setup said and asked."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.shown: list[str] = []
        super().__init__(ask=self._ask, say=self.shown.append)

    def _ask(self, question: str) -> str:
        self.shown.append(question)
        assert self.answers, f"setup asked more than the test answered: {question!r}"
        return self.answers.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.shown)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home, with config.toml's place inside it, and a neutral current folder."""
    fake = tmp_path / "fakeperson"
    fake.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(fake))
    for name in ("USER", "USERNAME", "LOGNAME"):
        monkeypatch.setenv(name, "fakeperson")
    monkeypatch.setattr(
        config_module, "user_config_file", lambda: fake / ".diffdata" / "config.toml"
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return fake


def settings(home: Path) -> Path:
    return home / ".diffdata" / "config.toml"


def no_picker(kind: str):
    return None


def fixed(char="k"):
    return lambda chars: char


def fresh_answers(channels: str = f"example-channel, {LINK}") -> list[str]:
    """Answers for a first setup with --team: use the team values, the default data folder,
    the channels, the workspace, the made-up handle, write, key later."""
    return ["", "", channels, "example.slack.com", "", "y", ""]


def run(home, *answers, team=TEAM, picker=no_picker, choose=None) -> tuple[int, Script]:
    script = Script(*answers)
    code = setup.setup(team=team, prompter=script, picker=picker, choose=choose or fixed())
    return code, script


def write_key(home: Path, mode: int = 0o600, data: dict | None = None) -> Path:
    key = home / ".diffdata" / "store-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(data or KEY), encoding="utf-8")
    os.chmod(key, mode)
    return key


EXISTING = """\
# My own comment.
[sources]
slack_channels = ["old-channel"]
slack_workspace_url = "https://example.slack.com"
slack_canvases = ["F00000001"]
canvas_refresh_days = 3
google_docs = ["1AbCdEfGhIjKlMnOpQrStUvWx"]
slack_opted_out = ["U00000001", "W00000002"]
folders = []

[local]
data_dir = "{data}"

[store]
backend = "gcs"
bucket = "example-bucket"
folder = "team-a"
producer = "saved-handle-1"
project = "example-project"
send_not_approved = "yes"
"""


def existing(home: Path) -> Path:
    path = settings(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXISTING.format(data=(home / "old-data").as_posix()), encoding="utf-8")
    return path


# --- a first setup -----------------------------------------------------------------------


def test_a_first_setup_writes_a_config_that_push_takes(home):
    code, script = run(home, *fresh_answers())
    assert code == 0
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["sources"]["slack_channels"] == ["example-channel", "C00000002"]
    assert raw["sources"]["slack_workspace_url"] == "https://example.slack.com"
    assert raw["local"]["data_dir"] == "~/diffdata-data"
    assert raw["store"] == {
        "backend": "gcs",
        "bucket": "example-bucket",
        "folder": "team-a",
        "project": "example-project",
        "producer": "example-project-kkkk",
        "send_not_approved": "no",
    }
    config = config_module.load_config(settings(home))
    assert config.slack_channel_ids == ("C00000002",)
    assert (home / "diffdata-data").is_dir()
    assert "/slack-export" in script.text


@POSIX
def test_the_settings_folder_config_and_data_folder_are_private(home):
    run(home, *fresh_answers())
    assert stat.S_IMODE(settings(home).stat().st_mode) == 0o600
    assert stat.S_IMODE(settings(home).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "diffdata-data").stat().st_mode) == 0o700


def test_the_handle_is_called_a_pseudonym_when_it_is_offered(home):
    _, script = run(home, *fresh_answers())
    assert PSEUDONYM in script.shown
    assert script.shown.index(PSEUDONYM) < next(
        i for i, line in enumerate(script.shown) if line.startswith("Producer handle")
    )


def test_a_no_at_the_last_question_writes_nothing(home):
    answers = fresh_answers()
    answers[5] = ""  # Write this? [y/N]
    code, script = run(home, *answers[:6])
    assert code == 0
    assert not settings(home).exists()
    assert not (home / "diffdata-data").exists()
    assert setup.NOTHING_WRITTEN in script.text


# --- must refuse 1: only a person at a terminal ------------------------------------------


def test_must_refuse_1_setup_wont_run_outside_a_terminal(home, capsys):
    assert cli.main(["setup", "--team", TEAM]) == 1
    assert "only runs in a terminal" in capsys.readouterr().err
    assert not settings(home).parent.exists()


def test_must_refuse_1_the_check_comes_before_anything_else(home, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(setup, "setup", lambda **kw: called.append(kw) or 0)
    assert cli.main(["setup"]) == 1
    assert called == []


# --- must refuse 2 and 3: no overwrite without a yes, no setting dropped -----------------


def test_must_refuse_2_an_existing_config_is_kept_without_a_yes(home):
    path = existing(home)
    before = path.read_bytes()
    code, script = run(home, "", "")  # Change it? [y/N]; key: later
    assert code == 0
    assert path.read_bytes() == before
    assert not (path.parent / setup.BACKUP_NAME).exists()
    assert "passes diffdata's checks" in script.text


def test_must_refuse_2_and_3_a_change_keeps_a_backup_and_every_other_setting(home):
    path = existing(home)
    before = path.read_bytes()
    old = tomllib.loads(before.decode("utf-8"))
    # Change: yes; team values from the old config: use; data folder: keep; channels: new;
    # workspace: keep; handle: keep; write: yes; key: later.
    code, _ = run(home, "y", "", "", "new-channel", "", "", "y", "", team=None)
    assert code == 0
    assert (path.parent / setup.BACKUP_NAME).read_bytes() == before
    new = tomllib.loads(path.read_text(encoding="utf-8"))
    assert new["sources"]["slack_channels"] == ["new-channel"]
    old["sources"]["slack_channels"] = ["new-channel"]
    assert new == old  # slack_opted_out, canvases, docs, refresh days, send_not_approved


def test_must_refuse_2_an_old_backup_is_replaced_only_on_a_second_yes(home):
    path = existing(home)
    backup = path.parent / setup.BACKUP_NAME
    backup.write_text("older backup\n", encoding="utf-8")
    before = path.read_bytes()
    code, script = run(home, "y", "", "", "new-channel", "", "", "y", "", team=None)
    assert code == 1
    assert path.read_bytes() == before
    assert backup.read_text(encoding="utf-8") == "older backup\n"
    assert setup.NOTHING_WRITTEN in script.text

    code, _ = run(home, "y", "", "", "new-channel", "", "", "y", "y", "", team=None)
    assert code == 0
    assert backup.read_bytes() == before


def test_must_refuse_3_a_config_that_cant_be_read_is_never_replaced(home):
    path = settings(home)
    path.parent.mkdir(parents=True)
    path.write_text("[sources\nslack_opted_out = ['U00000001']\n", encoding="utf-8")
    with pytest.raises(SetupError, match="can't be read as TOML"):
        run(home)
    assert "U00000001" in path.read_text(encoding="utf-8")


def test_must_refuse_3_a_setting_setup_cant_write_again_stops_it(home):
    path = existing(home)
    path.write_text(path.read_text(encoding="utf-8") + "[extra]\nwhen = 1979-05-27\n")
    before = path.read_bytes()
    with pytest.raises(SetupError, match="can't keep when under"):
        run(home, "y", "", "", "new-channel", "", "", "y", team=None)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "value",
    ['quote " and back\\slash', "tab\tand new\nline", "café ☕", "del\x7fchar", "", "#[x]"],
)
def test_must_refuse_3_text_reads_back_exactly(value):
    raw = {"top": value, "sources": {"slack_channels": [value], "odd key": value, "n": 3}}
    assert tomllib.loads(setup.render(raw)) == raw


# --- must refuse 4 and 5: no file the tool refuses, no half file -------------------------


def test_must_refuse_4_a_config_the_tool_would_refuse_is_never_written(home):
    path = existing(home)
    # A misspelt opt-out key: load_config() refuses it, since it would leave no one out.
    path.write_text(path.read_text(encoding="utf-8").replace("folders = []", "slack_optout = []"))
    before = path.read_bytes()
    with pytest.raises(SetupError, match="don't pass diffdata's checks"):
        run(home, "y", "", "", "new-channel", "", "", "y", team=None)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.toml", "config.toml.bak"]


def test_must_refuse_5_ctrl_c_while_writing_leaves_the_old_file(home, monkeypatch):
    path = existing(home)
    before = path.read_bytes()

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(home, "y", "", "", "new-channel", "", "", "y", team=None)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".config-*"))


# --- must refuse 6: only channels the person typed or pasted -----------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",  # none
        "a" * 81,  # too long
        "example/channel",
        "example.channel",
        "https://example.slack.com/docs/T00000001/F00000001",  # a canvas, not a channel
        f"{LINK}, https://other.slack.com/archives/C00000003",  # two workspaces
    ],
)
def test_must_refuse_6_a_channel_answer_that_isnt_channels_is_asked_again(home, bad):
    answers = fresh_answers()
    answers[2:3] = [bad, "example-channel"]
    code, script = run(home, *answers)
    assert code == 0
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == ["example-channel"]
    assert sum(line.startswith("Channels") for line in script.shown) == 2


def test_must_refuse_6_no_channel_comes_from_the_team_values(home):
    code, _ = run(home, *fresh_answers("example-channel"))
    assert code == 0
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == ["example-channel"]


def test_a_workspace_that_isnt_the_links_one_is_asked_again(home):
    answers = fresh_answers(LINK)
    answers[3:4] = ["https://other.slack.com", ""]
    code, script = run(home, *answers)
    assert code == 0
    assert "isn't the workspace your channel links are from" in script.text


@pytest.mark.parametrize("typed", ["example", "example.slack.com", "https://EXAMPLE.slack.com/"])
def test_the_workspace_address_can_be_typed_short(home, typed):
    answers = fresh_answers("example-channel")
    answers[3] = typed
    run(home, *answers)
    workspace = tomllib.loads(settings(home).read_text())["sources"]["slack_workspace_url"]
    assert workspace == "https://example.slack.com"


# --- must refuse 7: the data folder ------------------------------------------------------


@pytest.mark.parametrize("place", ["repo/data", "Dropbox/data"])
def test_must_refuse_7_a_chosen_folder_in_a_repo_or_synced_folder_is_refused(home, place):
    (home / "repo" / ".git").mkdir(parents=True)
    good = home / "good-data"
    picks = iter([home / place, good])
    answers = fresh_answers()
    answers[1:2] = ["c", ""]  # choose another; then use the good one
    code, script = run(home, *answers, picker=lambda kind: next(picks))
    assert code == 0
    assert "Choose another folder" in script.text
    assert tomllib.loads(settings(home).read_text())["local"]["data_dir"] == str(good)
    assert not (home / place).exists()


def test_must_refuse_7_a_refused_default_goes_straight_to_choosing(home, monkeypatch):
    synced = home / "OneDrive"
    synced.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(synced))
    typed = home / "typed-data"
    answers = fresh_answers()
    answers[1:2] = [str(typed), ""]  # no picker: typed path; then use it
    code, script = run(home, *answers)
    assert code == 0
    assert "OneDrive" in script.text
    assert typed.is_dir()


# --- must refuse 8: values push would refuse ---------------------------------------------


@pytest.mark.parametrize(
    ("team", "named"),
    [
        ("bucket=Bad_Bucket,folder=team-a,project=example-project", "bucket"),
        ("bucket=example-bucket,folder=team/a,project=example-project", "folder"),
        ("bucket=example-bucket,folder=team-a", "project"),
        ("bucket=example-bucket,folder=team-a,project=p,extra=x", "bucket=…"),
        ("bucket=example-bucket,bucket=b2,folder=team-a,project=p", "twice"),
    ],
)
def test_must_refuse_8_bad_team_values_are_refused_naming_the_key_only(home, team, named):
    with pytest.raises(SetupError, match=named) as e:
        run(home, team=team)
    for value in ("Bad_Bucket", "team/a", "b2"):
        assert value not in str(e.value)
    assert not settings(home).exists()


def test_must_refuse_8_a_bad_handle_is_asked_again(home):
    answers = fresh_answers()
    answers[4:5] = ["my name", "a/b", "team-a-1"]
    code, _ = run(home, *answers)
    assert code == 0
    assert tomllib.loads(settings(home).read_text())["store"]["producer"] == "team-a-1"


# --- must refuse 9 and 10: the store key -------------------------------------------------


@POSIX
def test_must_refuse_9_and_10_a_key_is_checked_and_never_shown(home):
    key = write_key(home, mode=0o644)
    before = key.read_bytes()
    answers = fresh_answers()
    answers[-1] = "n"  # chmod 600? no
    code, script = run(home, *answers)
    assert code == 0
    assert "Store key found." in script.text
    assert MARKER not in script.text and MARKER.lower() not in script.text
    assert stat.S_IMODE(key.stat().st_mode) == 0o644
    assert key.read_bytes() == before


@POSIX
def test_must_refuse_10_the_key_mode_changes_only_on_a_yes(home):
    key = write_key(home, mode=0o644)
    answers = fresh_answers()
    answers[-1] = "y"
    run(home, *answers)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_must_refuse_9_a_file_that_isnt_a_key_says_so_without_quoting_it(home):
    write_key(home, data={"type": "authorized_user", "refresh_token": MARKER})
    _, script = run(home, *fresh_answers()[:-1])
    assert "isn't a Google service account key" in script.text
    assert MARKER not in script.text


@POSIX
def test_setup_puts_a_downloaded_key_in_place(home):
    download = home / "Downloads" / "example-project-1234.json"
    download.parent.mkdir()
    download.write_text(json.dumps(KEY), encoding="utf-8")
    answers = fresh_answers()
    answers[-1:] = [str(download), "y"]  # the key's path; delete the download: yes
    code, script = run(home, *answers)
    assert code == 0
    key = home / ".diffdata" / "store-key.json"
    assert json.loads(key.read_text(encoding="utf-8")) == KEY
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not download.exists()
    assert MARKER not in script.text and MARKER.lower() not in script.text


def test_a_downloaded_file_that_isnt_a_key_is_refused_and_kept(home):
    download = home / "not-a-key.json"
    download.write_text(json.dumps({"type": "authorized_user", "x": MARKER}), encoding="utf-8")
    answers = fresh_answers()
    answers[-1:] = [str(download), ""]  # a wrong file, then "later"
    _, script = run(home, *answers)
    assert not (home / ".diffdata" / "store-key.json").exists()
    assert download.exists()
    assert MARKER not in script.text
    assert not list((home / ".diffdata").glob(".store-key-*"))


def test_a_downloaded_key_is_kept_without_a_yes(home):
    download = home / "key.json"
    download.write_text(json.dumps(KEY), encoding="utf-8")
    answers = fresh_answers()
    answers[-1:] = [str(download), ""]
    run(home, *answers)
    assert download.exists()


# --- must refuse 11 and 12: no config moved, nothing sent --------------------------------


def test_must_refuse_11_a_second_config_here_stops_setup_and_stays(home):
    here = Path.cwd() / "config.toml"
    here.write_text("# a config in the current folder\n", encoding="utf-8")
    with pytest.raises(SetupError, match="Move that file away or delete it yourself"):
        run(home)
    assert here.read_text(encoding="utf-8") == "# a config in the current folder\n"
    assert not settings(home).exists()


def test_must_refuse_12_setup_makes_no_network_call(home, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("setup opened a network connection")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    write_key(home)
    code, _ = run(home, *fresh_answers()[:-1])
    assert code == 0


# --- must refuse 13 and 14: the handle ---------------------------------------------------


def test_must_refuse_13_the_default_handle_is_made_up(home):
    run(home, *fresh_answers(), choose=setup.secrets.choice)
    handle = tomllib.loads(settings(home).read_text())["store"]["producer"]
    assert handle.startswith("example-project-") and len(handle) == len("example-project-") + 4
    assert "fakeperson" not in handle


def test_must_refuse_13_a_long_project_name_still_gives_a_handle_push_takes():
    handle = setup.new_handle("p" * 64, fixed())
    assert setup.push.SEGMENT.fullmatch(handle)


def test_must_refuse_14_a_saved_handle_stays_the_default(home):
    existing(home)
    run(home, "y", "", "", "new-channel", "", "", "y", "", team=None)
    assert tomllib.loads(settings(home).read_text())["store"]["producer"] == "saved-handle-1"


# --- must refuse 15 and 16: the team values ----------------------------------------------


def test_must_refuse_15_a_team_file_lying_around_is_never_read(home):
    for folder in (Path.cwd(), home / "Downloads", home / ".diffdata"):
        folder.mkdir(exist_ok=True)
        (folder / "diffdata-team.txt").write_text(
            "bucket=found-bucket,folder=found,project=found", encoding="utf-8"
        )
    answers = ["paste: " + TEAM, *fresh_answers()]
    answers[0] = TEAM  # the paste
    code, script = run(home, *answers, team=None)
    assert code == 0
    assert "found" not in settings(home).read_text(encoding="utf-8")
    assert "Paste the setup command" in script.text


def test_must_refuse_15_the_whole_pasted_command_works(home):
    answers = [f"uv run diffdata setup --team {TEAM}", *fresh_answers()]
    code, _ = run(home, *answers, team=None)
    assert code == 0
    assert tomllib.loads(settings(home).read_text())["store"]["bucket"] == "example-bucket"


def test_must_refuse_16_team_values_are_shown_and_need_a_yes(home):
    other = "bucket=other-bucket,folder=team-b,project=example-project"
    answers = ["n", other, *fresh_answers()]
    code, script = run(home, *answers)
    assert code == 0
    assert "bucket example-bucket, folder team-a" in script.text
    assert tomllib.loads(settings(home).read_text())["store"]["bucket"] == "other-bucket"


# --- team-command ------------------------------------------------------------------------


def test_team_command_prints_the_setup_command(capsys):
    args = ["--bucket", "example-bucket", "--folder", "team-a", "--project", "example-project"]
    assert cli.main(["team-command", *args]) == 0
    out = capsys.readouterr().out
    assert f"uv run diffdata setup --team {TEAM}" in out
    assert setup.parse_team(out.splitlines()[-1]) == {
        "bucket": "example-bucket",
        "folder": "team-a",
        "project": "example-project",
    }


def test_team_command_refuses_a_value_push_would_refuse(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = ["--bucket", "Bad_Bucket", "--folder", "team-a", "--project", "example-project"]
    assert cli.main(["team-command", *args]) == 1
    err = capsys.readouterr().err
    assert "bucket" in err and "Bad_Bucket" not in err
    assert list(tmp_path.iterdir()) == []
