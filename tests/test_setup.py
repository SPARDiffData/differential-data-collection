"""`diffdata setup` (#130, team file from #132). One test or more per must-refuse item.

Every test runs in a fake home: HOME and USERPROFILE point at a temporary folder, and
config.toml's place is inside it. No test reads the real ~/.diffdata/. The team file, its key
and its client file are made up, with a marker that must never show in what setup prints.
Names are examples only.
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

LINK = "https://example.slack.com/archives/C00000002"
MARKER = "MADE-UP-KEY-MARKER"
KEY = {
    "type": "service_account",
    "private_key": f"-----BEGIN PRIVATE KEY-----\n{MARKER}\n-----END PRIVATE KEY-----\n",
    "client_email": f"{MARKER.lower()}@example-project.iam.gserviceaccount.com",
    "project_id": MARKER.lower(),
}
CLIENT = {"installed": {"client_id": f"{MARKER}.apps.example", "client_secret": MARKER}}
VALUES = {"bucket": "example-bucket", "folder": "team-a", "project": "example-project"}
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


def team_file(home: Path, name="team-a", client=True, **changes) -> Path:
    """A made-up team file in the fake Downloads folder."""
    data = {"format": "diffdata-team", "version": 1, **VALUES, "store_key": KEY}
    if client:
        data["google_client"] = CLIENT
    data.update(changes)
    data = {k: v for k, v in data.items() if v is not None}
    path = home / "Downloads" / f"{name}.diffdata-team.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def settings(home: Path) -> Path:
    return home / ".diffdata" / "config.toml"


def no_picker(kind: str):
    return None


def fixed(char="k"):
    return lambda chars: char


def quick_answers(channels: str = f"example-channel, {LINK}") -> list[str]:
    """Answers for a first setup with a team file (revision 5): the channels, the workspace
    if no channel is a link, y on the summary screen, keep the team file."""
    space = [] if "/archives/" in channels else ["example.slack.com"]
    return [channels, *space, "y", "n"]


# A first setup's answers before c on the summary screen: valid channels, then c.
TO_STEPS = [f"example-channel, {LINK}", "c"]


def fresh_answers(channels: str = f"example-channel, {LINK}") -> list[str]:
    """Answers for a first setup with a team file, one question at a time (after c on the
    summary screen, which `run` answers first): use the team values, the data folder, the
    channels, the workspace, the handle, write, keep the team file."""
    return ["y", "", channels, "example.slack.com", "", "y", "n"]


def run(
    home, *answers, team="default", picker=no_picker, choose=None, steps=None
) -> tuple[int, Script]:
    """Run setup with these answers. On a first setup with a team file given, `steps` (the
    default) answers the summary screen with c first, so `answers` go through each question
    in turn, as fresh_answers() does. steps=False runs the short first setup as it is."""
    if team == "default":
        team = team_file(home)
    if steps is None:
        steps = team is not None and not settings(home).exists()
    if steps:
        answers = (*TO_STEPS, *answers)
    script = Script(*answers)
    code = setup.setup(team_file=team, prompter=script, picker=picker, choose=choose or fixed())
    return code, script


def write_key(home: Path, mode: int = 0o600, data: dict | None = None) -> Path:
    key = home / ".diffdata" / "store-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(data or KEY), encoding="utf-8")
    os.chmod(key, mode)
    return key


def no_marker(text: str) -> bool:
    return MARKER not in text and MARKER.lower() not in text


EXISTING = """\
# My own comment.
[sources]
slack_channels = ["old-channel"]
slack_workspace_url = "https://example.slack.com"
slack_canvases = ["F00000001"]
canvas_refresh_days = 3
google_docs = ["1AbCdEfGhIjKlMnOpQrStUvWx"]
slack_opted_out = ["U00000001", "W00000002"]
slack_notices.old-channel = "none"
slack_notices.C00000002 = "https://example.slack.com/archives/C00000002/p1790900000000000"
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

# A re-run with no team file: change it; use the old team values; keep the data folder; new
# channels; keep the workspace and the handle; write.
CHANGE = ["y", "y", "", "new-channel", "", "", "y"]


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
        **VALUES,
        "producer": "example-project-kkkk",
        "send_not_approved": "no",
    }
    config = config_module.load_config(settings(home))
    assert config.slack_channel_ids == ("C00000002",)
    assert setup.push.read_key(home / ".diffdata" / "store-key.json") == KEY
    assert json.loads((home / ".diffdata" / "google-client.json").read_text()) == CLIENT
    assert (home / "diffdata-data").is_dir()
    assert "/slack-export" in script.text


@POSIX
def test_the_settings_folder_config_and_data_folder_are_private(home):
    run(home, *fresh_answers())
    folder = settings(home).parent
    for name in ("config.toml", "store-key.json", "google-client.json"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "diffdata-data").stat().st_mode) == 0o700


def test_the_handle_is_called_a_pseudonym_when_it_is_offered(home):
    _, script = run(home, *fresh_answers())
    assert PSEUDONYM in script.shown
    assert script.shown.index(PSEUDONYM) < next(
        i for i, line in enumerate(script.shown) if line.startswith("Producer handle")
    )


def test_a_no_at_the_last_question_writes_nothing(home):
    answers = fresh_answers()
    answers[5] = "n"  # Write this?
    code, script = run(home, *answers[:6])
    assert code == 0
    assert not settings(home).parent.exists()
    assert not (home / "diffdata-data").exists()
    assert setup.NOTHING_WRITTEN in script.text


def test_a_team_file_with_no_google_client_still_works(home):
    code, _ = run(home, *fresh_answers(), team=team_file(home, client=False))
    assert code == 0
    assert (home / ".diffdata" / "store-key.json").exists()
    assert not (home / ".diffdata" / "google-client.json").exists()


def test_with_no_team_file_and_no_config_setup_asks_for_one(home):
    path = team_file(home)
    code, script = run(home, "", str(path), *quick_answers(), team=None)
    assert code == 0
    assert "Give the team file" in script.text
    assert sum(line.startswith("Team file's path") for line in script.shown) == 2


# --- must refuse 1: only a person at a terminal ------------------------------------------


def test_must_refuse_1_setup_wont_run_outside_a_terminal(home, capsys):
    assert cli.main(["setup", str(team_file(home))]) == 1
    assert "only runs in a terminal" in capsys.readouterr().err
    assert not settings(home).parent.exists()


def test_must_refuse_1_the_check_comes_before_anything_else(home, monkeypatch):
    called = []
    monkeypatch.setattr(setup, "setup", lambda **kw: called.append(kw) or 0)
    assert cli.main(["setup"]) == 1
    assert called == []


# --- must refuse 2 and 3: no overwrite without a yes, no setting dropped -----------------


def test_must_refuse_2_an_existing_config_is_kept_without_a_yes(home):
    path = existing(home)
    before = path.read_bytes()
    code, script = run(home, "n", team=None)  # Change it?
    assert code == 0
    assert path.read_bytes() == before
    assert not (path.parent / setup.BACKUP_NAME).exists()
    assert "passes diffdata's checks" in script.text


def test_must_refuse_2_a_team_file_isnt_used_without_changing_the_config(home):
    path = existing(home)
    before = path.read_bytes()
    code, script = run(home, "n", "n")  # Change it? no; put the key in place anyway? no
    assert code == 0
    assert path.read_bytes() == before
    assert "answer y to Change it" in script.text
    assert not (home / ".diffdata" / "store-key.json").exists()


def test_must_refuse_2_and_3_a_change_keeps_a_backup_and_every_other_setting(home):
    path = existing(home)
    before = path.read_bytes()
    old = tomllib.loads(before.decode("utf-8"))
    code, _ = run(home, *CHANGE, team=None)
    assert code == 0
    assert (path.parent / setup.BACKUP_NAME).read_bytes() == before
    new = tomllib.loads(path.read_text(encoding="utf-8"))
    assert new["sources"]["slack_channels"] == ["new-channel"]
    old["sources"]["slack_channels"] = ["new-channel"]
    assert new == old  # slack_opted_out, slack_notices, canvases, docs, refresh days, ...
    assert new["sources"]["slack_notices"]["old-channel"] == "none"  # kept exactly (#118)


def test_a_rerun_says_typed_channels_replace_the_list(home):
    existing(home)
    _, script = run(home, *CHANGE, team=None)
    assert "what you type replaces this list" in script.text


def test_must_refuse_2_an_old_backup_is_replaced_only_on_a_second_yes(home):
    path = existing(home)
    backup = path.parent / setup.BACKUP_NAME
    backup.write_text("older backup\n", encoding="utf-8")
    before = path.read_bytes()
    code, script = run(home, *CHANGE, "n", team=None)
    assert code == 1
    assert path.read_bytes() == before
    assert backup.read_text(encoding="utf-8") == "older backup\n"
    assert setup.NOTHING_WRITTEN in script.text

    code, _ = run(home, *CHANGE, "y", team=None)
    assert code == 0
    assert backup.read_bytes() == before


@POSIX
def test_the_backup_is_private_and_replaces_the_old_one_in_one_step(home):
    path = existing(home)
    backup = path.parent / setup.BACKUP_NAME
    backup.write_text("older backup\n", encoding="utf-8")
    os.chmod(backup, 0o644)
    before = path.read_bytes()
    run(home, *CHANGE, "y", team=None)
    assert backup.read_bytes() == before
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".config-bak-*"))


@POSIX
def test_a_backup_that_is_a_link_is_refused_and_its_target_kept(home):
    path = existing(home)
    target = home / "precious.txt"
    target.write_text("keep me\n", encoding="utf-8")
    (path.parent / setup.BACKUP_NAME).symlink_to(target)
    before = path.read_bytes()
    with pytest.raises(SetupError, match="is a link or a folder"):
        run(home, *CHANGE, team=None)
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert path.read_bytes() == before


@POSIX
def test_a_config_that_is_a_link_is_never_written_through(home):
    real = home / "elsewhere.toml"
    real.write_text(EXISTING.format(data=(home / "old-data").as_posix()), encoding="utf-8")
    settings(home).parent.mkdir(parents=True)
    settings(home).symlink_to(real)
    before = real.read_bytes()
    with pytest.raises(SetupError, match="is a link"):
        run(home, *CHANGE, team=None)
    assert real.read_bytes() == before
    assert settings(home).is_symlink()


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
        run(home, *CHANGE, team=None)
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
        run(home, *CHANGE, team=None)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.toml", "config.toml.bak"]


def test_must_refuse_5_ctrl_c_while_writing_leaves_the_old_file(home, monkeypatch):
    path = existing(home)
    before = path.read_bytes()

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(home, *CHANGE, team=None)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".config-*"))


def test_an_os_error_after_the_write_says_the_config_was_written(home, monkeypatch):
    def cant_make(*args, **kwargs):
        raise PermissionError("made-up refusal")

    answers = fresh_answers()
    monkeypatch.setattr(setup, "install_team_file", cant_make)
    with pytest.raises(SetupError, match="was written, but then something failed"):
        run(home, *answers[:6])
    assert settings(home).exists()


# --- must refuse 6 and 17: only channels, typed or pasted --------------------------------


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
    answers = quick_answers("example-channel")
    answers[0:1] = [bad, "example-channel"]
    code, script = run(home, *answers, steps=False)
    assert code == 0
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == ["example-channel"]
    assert sum(line.startswith("Channels") for line in script.shown) == 2


@pytest.mark.parametrize(
    "dm",
    [
        "D00000001",  # a DM's ID
        "https://example.slack.com/archives/D00000001",
        "https://example.slack.com/archives/D00000001/p1700000000000100",
        "mpdm-alex--sam--kim-1",  # Slack's name for a group DM
        "#MPDM-alex--sam-1",
    ],
)
def test_must_refuse_17_a_dm_or_group_dm_is_never_listed(home, dm):
    answers = fresh_answers()
    answers[2:3] = [dm, "example-channel"]
    code, script = run(home, *answers)
    assert code == 0
    assert "direct message or a group DM" in script.text and "CON-5" in script.text
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == ["example-channel"]


def test_must_refuse_6_no_channel_comes_from_the_team_file(home):
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
    path = team_file(home)
    synced = home / "OneDrive"
    synced.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(synced))
    typed = home / "typed-data"
    answers = fresh_answers()
    answers[1:2] = [str(typed), ""]  # no picker: typed path; then use it
    code, script = run(home, *answers, team=path)
    assert code == 0
    assert "OneDrive" in script.text
    assert typed.is_dir()


def test_a_typed_relative_data_folder_is_saved_as_an_absolute_path(home):
    answers = fresh_answers()
    answers[1:2] = ["c", "../relative-data", ""]  # choose; no picker: type it; then use it
    code, _ = run(home, *answers)
    assert code == 0
    saved = tomllib.loads(settings(home).read_text())["local"]["data_dir"]
    assert saved == str((Path.cwd().parent / "relative-data").resolve())


def test_a_data_folder_that_is_a_file_is_asked_again(home):
    a_file = home / "a-file"
    a_file.write_text("x", encoding="utf-8")
    answers = fresh_answers()
    answers[1:2] = ["c", str(a_file), str(home / "good"), ""]  # refused: straight to typing
    code, script = run(home, *answers)
    assert code == 0
    assert "is a file, not a folder" in script.text
    assert tomllib.loads(settings(home).read_text())["local"]["data_dir"] == str(home / "good")


def test_a_settings_folder_in_a_synced_folder_is_refused_before_anything(home, monkeypatch):
    path = team_file(home)
    synced = home / "Dropbox" / ".diffdata" / "config.toml"
    monkeypatch.setattr(config_module, "user_config_file", lambda: synced)
    with pytest.raises(SetupError, match="synced folder"):
        run(home, team=path)
    assert not (home / "Dropbox").exists()


# --- must refuse 8: values push would refuse, and a team file that isn't one -------------


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"bucket": "Bad_Bucket"}, "bucket"),
        ({"folder": "team/a"}, "folder"),
        ({"project": None}, "no project"),
        ({"store_key": None}, "no store_key"),
        ({"extra": "x"}, "doesn't know"),
        ({"version": 2}, "version 1"),
        ({"version": True}, "version 1"),
        ({"format": "something-else"}, "isn't a diffdata team file"),
        ({"bucket": 3}, "must be text"),
        ({"store_key": {"type": "authorized_user", "refresh_token": MARKER}}, "store key"),
        ({"store_key": {**KEY, "token_uri": "https://example.com/token"}}, "store key"),
        ({"google_client": {"web": {"client_id": MARKER}}}, "Desktop app client"),
    ],
)
def test_must_refuse_8_a_bad_team_file_is_refused_without_quoting_it(home, changes, named):
    with pytest.raises(SetupError, match=named) as e:
        run(home, team=team_file(home, **changes))
    for value in ("Bad_Bucket", "team/a", "something-else"):
        assert value not in str(e.value)
    assert no_marker(str(e.value))
    assert not settings(home).parent.exists()


# Short ids: a test's id goes in an environment variable, which Windows caps at 32,767 characters.
@pytest.mark.parametrize(
    "content", ["not json", "[1, 2]", "x" * 300_000], ids=["not json", "a list", "too big"]
)
def test_must_refuse_8_a_file_that_isnt_json_or_is_too_big_is_refused(home, content):
    path = home / "Downloads" / "odd.diffdata-team.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SetupError, match="team file"):
        run(home, team=path)


@POSIX
def test_must_refuse_8_a_team_file_that_is_a_link_is_refused(home):
    real = team_file(home)
    link = home / "link.diffdata-team.json"
    link.symlink_to(real)
    with pytest.raises(SetupError, match="not a folder or a link"):
        run(home, team=link)


def test_must_refuse_8_a_bad_handle_is_asked_again(home):
    answers = fresh_answers()
    answers[4:5] = ["my name", "a/b", "team-a-1"]
    code, _ = run(home, *answers)
    assert code == 0
    assert tomllib.loads(settings(home).read_text())["store"]["producer"] == "team-a-1"


# --- must refuse 9, 10 and 18: the key and the client file -------------------------------


def test_must_refuse_9_and_18_nothing_from_the_key_or_client_file_is_shown(home, capsys):
    _, script = run(home, *fresh_answers())
    assert no_marker(script.text)
    assert no_marker(capsys.readouterr().out)


@POSIX
def test_must_refuse_10_a_key_others_can_read_changes_only_on_a_typed_yes(home):
    existing(home)
    key = write_key(home, mode=0o644)
    run(home, "n", "", "n", team=None)  # Change it? no; chmod? Enter asks again, then no
    assert stat.S_IMODE(key.stat().st_mode) == 0o644
    run(home, "n", "y", team=None)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_must_refuse_18_a_different_key_is_replaced_only_on_a_typed_yes(home):
    other = {**KEY, "client_email": "other@example-project.iam.gserviceaccount.com"}
    key = write_key(home, data=other)
    before = key.read_bytes()
    answers = fresh_answers()
    answers[-1:] = ["n"]  # replace the key? no: so no delete question
    _, script = run(home, *answers)
    assert key.read_bytes() == before
    assert "Kept the team file" in script.text

    settings(home).unlink()
    answers = fresh_answers()
    answers[-1:] = ["y", "n"]
    run(home, *answers)
    assert setup.push.read_key(key) == KEY


def test_must_refuse_18_the_team_file_is_deleted_only_on_a_typed_yes(home):
    path = team_file(home)
    run(home, *fresh_answers(), team=path)
    assert path.exists()

    settings(home).unlink()
    answers = fresh_answers()
    answers[-1] = "y"
    run(home, *answers, team=path)
    assert not path.exists()


@POSIX
def test_must_refuse_18_a_key_place_that_is_a_link_is_never_written_through(home):
    target = home / "precious.txt"
    target.write_text("keep me\n", encoding="utf-8")
    folder = home / ".diffdata"
    folder.mkdir()
    (folder / "store-key.json").symlink_to(target)
    team = team_file(home)
    _, script = run(home, *fresh_answers()[:-1], team=team)  # no delete question
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert "store key wasn't put there" in script.text
    assert team.exists()


def test_must_refuse_18_a_key_write_cut_short_leaves_no_key(home, monkeypatch):
    key = home / ".diffdata" / "store-key.json"
    real_open, real_fdopen = setup.os.open, setup.os.fdopen
    key_fds = []

    def open_and_note(path, flags, *args):
        fd = real_open(path, flags, *args)
        if Path(path) == key:
            key_fds.append(fd)
        return fd

    class CutShort:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.close(self.fd)

        def write(self, data):
            raise KeyboardInterrupt

    def fdopen(fd, *args, **kwargs):
        return CutShort(fd) if fd in key_fds else real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(setup.os, "open", open_and_note)
    monkeypatch.setattr(setup.os, "fdopen", fdopen)
    with pytest.raises(KeyboardInterrupt):
        run(home, *fresh_answers()[:6])
    assert key_fds and not key.exists()


def test_with_no_key_a_rerun_says_it_comes_in_the_team_file(home):
    existing(home)
    _, script = run(home, "n", team=None)
    assert "It comes in the team file" in script.text


# --- must refuse 11 and 12: no config moved, nothing sent --------------------------------


def test_must_refuse_11_a_second_config_here_stops_setup_and_stays(home):
    path = team_file(home)
    here = Path.cwd() / "config.toml"
    here.write_text("# a config in the current folder\n", encoding="utf-8")
    with pytest.raises(SetupError, match="Move that file away or delete it yourself"):
        run(home, team=path)
    assert here.read_text(encoding="utf-8") == "# a config in the current folder\n"
    assert not settings(home).exists()


def test_must_refuse_12_setup_makes_no_network_call(home, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("setup opened a network connection")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    code, _ = run(home, *fresh_answers())
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
    run(home, *CHANGE, team=None)
    assert tomllib.loads(settings(home).read_text())["store"]["producer"] == "saved-handle-1"


# --- must refuse 15 and 16: the team values ----------------------------------------------


def test_must_refuse_15_a_team_file_lying_around_is_never_read(home):
    for folder in (Path.cwd(), home / "Downloads", home / ".diffdata"):
        folder.mkdir(exist_ok=True)
        (folder / "found.diffdata-team.json").write_text(
            json.dumps({"format": "diffdata-team", "version": 1, "bucket": "found-bucket"}),
            encoding="utf-8",
        )
    path = team_file(home)
    code, script = run(home, str(path), *quick_answers(), team=None)
    assert code == 0
    assert "found" not in settings(home).read_text(encoding="utf-8")
    assert "Give the team file" in script.text


def test_must_refuse_15_a_rerun_offers_the_values_setup_wrote_before(home):
    existing(home)
    _, script = run(home, *CHANGE, team=None)
    assert "Your config.toml has these team values:" in script.text


def test_must_refuse_16_team_values_need_a_typed_yes(home):
    other = team_file(home, name="team-b", folder="team-b")
    # Enter alone asks again; no: give another file; yes to its values.
    answers = ["", "n", str(other), "y", *fresh_answers()[1:]]
    code, script = run(home, *answers)
    assert code == 0
    assert "Please type y or n." in script.text
    assert "bucket example-bucket, folder team-a" in script.text
    assert tomllib.loads(settings(home).read_text())["store"]["folder"] == "team-b"


def test_team_command_and_the_team_flag_are_gone(capsys):
    with pytest.raises(SystemExit):
        cli.main(["team-command", "--bucket", "x", "--folder", "y", "--project", "z"])
    with pytest.raises(SystemExit):
        cli.main(["setup", "--team", "bucket=x,folder=y,project=z"])


# --- revision 5: the short first setup ------------------------------------------------------


def asked(script: Script, start: str) -> int:
    return sum(line.startswith(start) for line in script.shown)


def test_a_short_first_setup_takes_three_answers(home):
    code, script = run(home, *quick_answers(), steps=False)
    assert code == 0
    assert script.answers == []  # channels, y on the screen, keep the team file: nothing more
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["sources"]["slack_channels"] == ["example-channel", "C00000002"]
    assert raw["sources"]["slack_workspace_url"] == "https://example.slack.com"
    assert raw["local"]["data_dir"] == setup.DEFAULT_DATA_DIR
    assert {k: raw["store"][k] for k in VALUES} == VALUES
    assert raw["store"]["producer"] == "example-project-kkkk"
    assert raw["store"]["send_not_approved"] == "no"
    assert (home / "diffdata-data").is_dir()
    assert (home / ".diffdata" / "store-key.json").exists()
    for question in ("Use these?", "Workspace", "Producer handle"):
        assert asked(script, question) == 0
    assert "Press Enter to use it" not in script.text  # the data folder question


def test_must_refuse_19_the_screen_shows_every_chosen_value_before_the_yes(home):
    path = team_file(home)
    _, script = run(home, *quick_answers(), team=path, steps=False)
    question = next(i for i, line in enumerate(script.shown) if line.startswith("Write this?"))
    before = "\n".join(script.shown[:question])
    for shown in (
        "example-channel, C00000002 (a channel ID)",
        "https://example.slack.com",
        str(home / "diffdata-data"),
        "bucket example-bucket, folder team-a, project example-project",
        f"from the team file {path}",
        "a store key and a Google client file",
        "example-project-kkkk (a pseudonym)",
        PSEUDONYM,
    ):
        assert shown in before, shown
    assert no_marker(script.text)


def test_must_refuse_16_n_on_the_screen_writes_nothing(home):
    path = team_file(home)
    code, script = run(home, f"example-channel, {LINK}", "n", team=path, steps=False)
    assert code == 0
    assert setup.NOTHING_WRITTEN in script.text
    assert not settings(home).exists()
    assert not (home / ".diffdata" / "store-key.json").exists()
    assert not (home / "diffdata-data").exists()
    assert path.exists()


@pytest.mark.parametrize("other", ["", "yes", "x", "write", "Y N"])
def test_must_refuse_16_only_y_n_or_c_answers_the_screen(home, other):
    code, script = run(home, f"example-channel, {LINK}", other, "n", steps=False)
    assert code == 0
    assert asked(script, "Write this?") == 2
    assert "Please type y, n or c." in script.text
    assert not settings(home).exists()


def test_c_asks_each_question_with_the_shown_values_then_shows_the_screen_again(home):
    # c; then yes to the team values and Enter at every question; then y; keep the team file.
    answers = [f"example-channel, {LINK}", "c", "y", "", "", "", "", "y", "n"]
    code, script = run(home, *answers, steps=False)
    assert code == 0
    assert asked(script, "Write this?") == 2
    assert asked(script, "Use these?") == 1
    assert "Channels [Enter keeps: example-channel, C00000002" in script.text
    assert "Producer handle [Enter for example-project-kkkk]" in script.text
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["sources"]["slack_channels"] == ["example-channel", "C00000002"]
    assert raw["store"]["producer"] == "example-project-kkkk"


def test_c_changes_a_value_and_n_on_the_second_screen_writes_nothing(home):
    answers = [f"example-channel, {LINK}", "c", "y", "", "other-channel", "", "", "n"]
    code, script = run(home, *answers, steps=False)
    assert code == 0
    assert "  Channels:         other-channel" in script.text
    assert not settings(home).exists()


def test_with_no_channel_link_the_workspace_is_asked(home):
    code, script = run(home, *quick_answers("example-channel"), steps=False)
    assert code == 0
    assert asked(script, "Workspace address") == 1
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["sources"]["slack_workspace_url"] == "https://example.slack.com"


def test_a_refused_default_data_folder_is_asked_before_the_screen(home):
    (home / "diffdata-data").write_text("not a folder", encoding="utf-8")
    good = home / "good-data"
    # No picker: type the path; it's shown again; Enter uses it. Then the short setup.
    code, script = run(home, str(good), "", *quick_answers(), steps=False)
    assert code == 0
    assert "is a file, not a folder" in script.text
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["local"]["data_dir"] == str(good.resolve())


def test_a_rerun_still_asks_each_question_and_has_no_c(home):
    existing(home)
    before = settings(home).read_bytes()
    _, script = run(home, *CHANGE[:-1], "c", "n", team=None)
    assert asked(script, "Write this to") == 2  # c isn't an answer there: asked again
    assert asked(script, "Write this?") == 0
    assert settings(home).read_bytes() == before


def test_a_config_that_goes_away_during_change_it_stays_a_rerun(home):
    path = existing(home)

    class Moving(Script):
        def _ask(self, question):
            if question.startswith("Change it?"):
                path.rename(path.with_name("moved.toml"))
            return super()._ask(question)

    script = Moving("y", *CHANGE[1:])
    setup.setup(team_file=None, prompter=script, picker=no_picker, choose=fixed())
    assert "Your config.toml has these team values:" in script.text
    assert asked(script, "Write this?") == 0
    assert "slack_opted_out" in path.read_text(encoding="utf-8")


def test_a_link_with_a_bad_workspace_asks_for_the_channels_again(home):
    bad = "https://-example.slack.com/archives/C00000002"
    code, script = run(home, bad, *quick_answers(), steps=False)
    assert code == 0
    assert asked(script, "Channels") == 2
    raw = tomllib.loads(settings(home).read_text(encoding="utf-8"))
    assert raw["sources"]["slack_workspace_url"] == "https://example.slack.com"


def test_a_bad_link_workspace_after_c_asks_for_the_channels_again(home):
    bad = "https://-example.slack.com/archives/C00000002"
    # c; yes to the team values; data folder; a bad link, then a good one; workspace; handle.
    answers = [f"example-channel, {LINK}", "c", "y", "", bad, LINK, "", "", "y", "n"]
    code, script = run(home, *answers, steps=False)
    assert code == 0
    assert "Give the channels again." in script.text


def test_printable_replaces_control_characters():
    assert setup.printable(Path("team\x1b[2Ja\x07.json")) == "team?[2Ja?.json"


@POSIX  # Windows refuses control characters in file names
def test_a_team_file_path_with_control_characters_is_shown_safely(home):
    path = team_file(home, name="team\x1b[2Ja")
    _, script = run(home, *quick_answers(), team=path, steps=False)
    assert "\x1b" not in script.text
    assert "team?[2Ja.diffdata-team.json" in script.text


def test_a_first_run_never_reads_a_config(home, monkeypatch):
    calls = []
    real = setup.read_raw
    monkeypatch.setattr(setup, "read_raw", lambda p: calls.append(p) or real(p))
    code, _ = run(home, *quick_answers(), steps=False)
    assert code == 0
    assert calls == []  # a first run never reads a config


# --- round 3: explicit y/n, key install on "no change", capital names ----------------------


def test_a_safety_question_takes_only_y_or_n():
    script = Script("", "maybe", "Y", "", "no")
    assert script.yes("Go?") is True
    assert script.yes("Go?") is False
    assert script.text.count("Please type y or n.") == 3


@pytest.mark.parametrize(
    ("where", "question", "steps"),
    [
        (1, "Write this?", False),  # the summary screen
        (2, "Delete the team file", False),
        (0, "Use these?", True),  # after c
        (6, "Delete the team file", True),
    ],
)
def test_must_refuse_16_enter_at_a_first_setup_question_asks_again(home, where, question, steps):
    answers = quick_answers() if not steps else fresh_answers()
    answers.insert(where, "")
    code, script = run(home, *answers, steps=steps)
    assert code == 0
    asked = [line for line in script.shown if line.startswith(question)]
    assert len(asked) == 2


def test_enter_at_change_it_asks_again(home):
    path = existing(home)
    before = path.read_bytes()
    _, script = run(home, "", "n", team=None)
    assert sum(line.startswith("Change it?") for line in script.shown) == 2
    assert path.read_bytes() == before


def test_enter_at_replacing_an_old_backup_asks_again(home):
    path = existing(home)
    (path.parent / setup.BACKUP_NAME).write_text("older backup\n", encoding="utf-8")
    _, script = run(home, *CHANGE, "", "n", team=None)
    assert sum(line.startswith("There's an older") for line in script.shown) == 2


def test_enter_at_replacing_a_key_asks_again(home):
    other = {**KEY, "client_email": "other@example-project.iam.gserviceaccount.com"}
    key = write_key(home, data=other)
    before = key.read_bytes()
    answers = fresh_answers()
    answers[-1:] = ["", "n"]  # replace? Enter asks again, then no (so no delete question)
    _, script = run(home, *answers)
    assert sum(line.startswith("There's a different store key") for line in script.shown) == 2
    assert key.read_bytes() == before


def test_with_no_change_the_team_files_parts_can_still_be_put_in_place(home):
    path = existing(home)
    before = path.read_bytes()
    team = team_file(home)
    # Change it? no; put the key and client file in place anyway? yes; delete? no
    code, script = run(home, "n", "y", "n", team=team)
    assert code == 0
    assert path.read_bytes() == before  # the team values weren't used (16)
    assert setup.push.read_key(home / ".diffdata" / "store-key.json") == KEY
    assert (home / ".diffdata" / "google-client.json").exists()
    assert team.exists()
    assert no_marker(script.text)


def test_with_no_change_and_a_no_the_team_file_is_left_alone(home):
    existing(home)
    team = team_file(home)
    run(home, "n", "n", team=team)
    assert not (home / ".diffdata" / "store-key.json").exists()
    assert team.exists()


@pytest.mark.parametrize("name", ["GENERAL", "CAREERS", "DESIGNS", "GROUPCHAT"])
def test_a_name_typed_in_capitals_stays_a_name(home, name):
    answers = fresh_answers(name)
    code, _ = run(home, *answers)
    assert code == 0
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == [name.lower()]


def test_with_no_change_a_matching_team_file_says_it_matches(home):
    existing(home)  # bucket example-bucket, folder team-a, project example-project
    _, script = run(home, "n", "n", team=team_file(home))
    assert "bucket example-bucket, folder team-a, project example-project" in script.text
    assert "These match your config.toml." in script.text
    assert "push will fail" not in script.text


def test_with_no_change_a_team_file_for_another_folder_warns_before_the_offer(home):
    path = existing(home)
    before = path.read_bytes()
    other = team_file(home, name="team-b", folder="team-b")
    # Change it? no; put in place anyway? Enter asks again, then no
    _, script = run(home, "n", "", "n", team=other)
    text = script.text
    assert "folder team-b" in text
    assert "don't match" in text and "push will fail" in text and "answer y to Change it" in text
    assert text.index("push will fail") < text.index("in place anyway?")
    assert sum(line.startswith("Put the team file's") for line in script.shown) == 2
    assert path.read_bytes() == before
    assert not (home / ".diffdata" / "store-key.json").exists()


def test_an_id_typed_by_hand_is_marked_and_lowercase_is_suggested(home):
    _, script = run(home, *fresh_answers(f"GROUP2026, {LINK}, example-channel"))
    text = script.text
    assert "GROUP2026 is stored as a channel ID" in text and "lowercase" in text
    assert "C00000002 is stored as a channel ID" not in text  # from a link: no note
    assert "GROUP2026 (a channel ID), C00000002 (a channel ID), example-channel" in text
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == ["GROUP2026", "C00000002", "example-channel"]


# --- round 6: the team file is kept until every part of it is in place ------------------


def test_must_refuse_18_a_kept_different_key_keeps_the_team_file(home):
    other = {**KEY, "client_email": "other@example-project.iam.gserviceaccount.com"}
    write_key(home, data=other)
    team = team_file(home)
    answers = fresh_answers()
    answers[-1:] = ["n"]  # replace the key? no
    _, script = run(home, *answers, team=team)
    assert team.exists()
    assert "Kept the team file" in script.text and "store key is not in place" in script.text
    assert not any(line.startswith("Delete the team file") for line in script.shown)


def test_must_refuse_18_a_kept_client_file_keeps_the_team_file(home):
    folder = home / ".diffdata"
    folder.mkdir()
    (folder / "google-client.json").write_text('{"installed": {"client_id": "other"}}\n')
    team = team_file(home)
    answers = fresh_answers()
    answers[-1:] = ["n"]  # replace the client file? no
    _, script = run(home, *answers, team=team)
    assert team.exists()
    assert setup.push.read_key(folder / "store-key.json") == KEY  # the key went in
    assert "Google client file is not in place" in script.text
    assert not any(line.startswith("Delete the team file") for line in script.shown)


def test_with_every_part_already_the_same_the_delete_is_offered(home):
    team = team_file(home)
    run(home, *fresh_answers(), team=team)  # puts both in place; keeps the file
    settings(home).unlink()
    answers = fresh_answers()
    answers[-1] = "y"
    _, script = run(home, *answers, team=team)
    assert "is the same as the team file's" in script.text
    assert not team.exists()


def test_a_dm_shaped_capitals_entry_in_config_says_to_retype_in_lowercase(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = tmp_path / "config.toml"
    path.write_text(
        f'[sources]\nslack_channels = ["DESIGN2024"]\n[local]\ndata_dir = "{data}"\n',
        encoding="utf-8",
    )
    with pytest.raises(config_module.ConfigError, match="type it in lowercase") as e:
        config_module.load_config(path)
    assert "Entry 1" in str(e.value) and "DESIGN2024" not in str(e.value)


# --- round 7: JSON comparison, lowercase hint, typed data folder ------------------------


def test_a_hand_saved_key_with_the_same_contents_counts_as_the_same(home):
    key = home / ".diffdata" / "store-key.json"
    key.parent.mkdir()
    key.write_text(json.dumps(KEY, separators=(",", ":")), encoding="utf-8")  # no newline
    os.chmod(key, 0o600)  # so there's no chmod question
    before = key.read_bytes()
    answers = fresh_answers()
    answers[-1] = "n"  # no replace question; delete the team file? no
    _, script = run(home, *answers)
    assert "is the same as the team file's" in script.text
    assert not any(line.startswith("There's a different") for line in script.shown)
    assert key.read_bytes() == before  # left as it was


def test_a_key_with_different_contents_is_still_different(home):
    write_key(home, data={**KEY, "private_key_id": "another"})
    answers = fresh_answers()
    answers[-1:] = ["n"]  # replace? no
    _, script = run(home, *answers)
    assert any(line.startswith("There's a different store key") for line in script.shown)


def test_a_hand_saved_client_file_with_the_same_contents_counts_as_the_same(home):
    folder = home / ".diffdata"
    folder.mkdir()
    (folder / "google-client.json").write_text(json.dumps(CLIENT, indent=4), encoding="utf-8")
    answers = fresh_answers()
    _, script = run(home, *answers)
    assert "The Google client file at" in script.text and "is the same" in script.text


@pytest.mark.parametrize("entry", ["DESIGN2024", "DEV1TEAM"])
def test_a_dm_shaped_capitals_entry_at_setup_says_to_type_it_in_lowercase(home, entry):
    answers = fresh_answers()
    answers[2:3] = [entry, entry.lower()]
    code, script = run(home, *answers)
    assert code == 0
    assert "type it in lowercase" in script.text
    channels = tomllib.loads(settings(home).read_text())["sources"]["slack_channels"]
    assert channels == [entry.lower()]


def test_a_real_dm_link_gets_no_lowercase_hint(home):
    answers = fresh_answers()
    answers[2:3] = ["https://example.slack.com/archives/D00000001", "example-channel"]
    _, script = run(home, *answers)
    assert "direct message" in script.text and "lowercase" not in script.text


def test_a_path_typed_at_the_data_folder_prompt_is_checked_and_used(home):
    good = home / "typed-here"
    answers = fresh_answers()
    answers[1:2] = [str(good), ""]  # a typed path; it's shown again; Enter uses it
    code, _ = run(home, *answers)
    assert code == 0
    assert tomllib.loads(settings(home).read_text())["local"]["data_dir"] == str(good)
    assert good.is_dir()


@pytest.mark.parametrize("place", ["repo/data", "Dropbox/data"])
def test_a_path_typed_at_the_data_folder_prompt_gets_the_same_checks(home, place):
    (home / "repo" / ".git").mkdir(parents=True)
    good = home / "good-data"
    answers = fresh_answers()
    # A refused typed path goes to choosing: no picker, so a typed path again; then Enter.
    answers[1:2] = [str(home / place), str(good), ""]
    code, script = run(home, *answers)
    assert code == 0
    assert "Choose another folder" in script.text
    assert tomllib.loads(settings(home).read_text())["local"]["data_dir"] == str(good)
    assert not (home / place).exists()
