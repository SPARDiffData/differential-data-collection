"""`diffdata admin optouts` and the export's day-7 check and list fetch (#132 PR 3).

One test or more per must-refuse item 9 to 12, with a fixed clock. `gcloud` and the store are
faked: nothing here reaches Google. Every ID, name and time is made up.
"""

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common.config import Config, ConfigError, load_config, team_opted_out_path
from diffdata.discover import team_optouts
from diffdata.discover.slack_inbox import SlackExportError, begin, run_path
from diffdata.store import admin
from diffdata.store.admin import AdminError
from diffdata.store.backend import StoreError

CHANNEL = "C00000001"
OTHER = "C00000002"
NOTICE_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOTICED = f"{CHANNEL}/{int(NOTICE_AT.timestamp())}.000100"
DAY_7 = NOTICE_AT + timedelta(days=7, seconds=1)  # the ts's fraction is after the second
LIST = "fake-team/_config/opted-out.json"
URL = "gs://fake-bucket/fake-team/_config/opted-out.json"
KEY = {
    "type": "service_account",
    "client_email": "fake-uploader@fake-project.iam.gserviceaccount.com",
    "private_key": "FAKE-NOT-A-KEY",
}
POSIX = pytest.mark.skipif(os.name == "nt", reason="admin runs on macOS and Linux only")


def team_config(tmp_path: Path, **changes) -> Config:
    """A producer's settings with a team folder, and the team's key next to config.toml."""
    home = tmp_path / "fake-diffdata"
    home.mkdir(exist_ok=True)
    (home / "store-key.json").write_text(json.dumps(KEY), encoding="utf-8")
    config = Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel", "other-channel"),
        slack_notices=(("example-channel", NOTICED), ("other-channel", "none")),
        slack_workspace_url="https://example.slack.com",
        store_bucket="fake-bucket",
        store_folder="fake-team",
        path=home / "config.toml",
    )
    return replace(config, **changes)


def start(config, when, channels=(f"example-channel={CHANNEL}",)):
    return begin(config, list(channels), now=when)


# --- item 10: the 7-day window ----------------------------------------------------------------


def test_must_refuse_an_export_on_day_6(tmp_path, team_lists):
    config = team_config(tmp_path)
    with pytest.raises(SlackExportError, match="window is still open") as e:
        start(config, NOTICE_AT + timedelta(days=6, hours=23))
    assert "2026-10-08 12:00 UTC" in str(e.value)
    assert not run_path(config.data_dir).exists()  # no run opened
    assert team_lists.asked == []  # nor the list fetched


def test_an_export_on_day_7_starts(tmp_path, team_lists):
    report = start(team_config(tmp_path), DAY_7)
    assert "Fetched your team's opt-out list: 0 person(s)" in report
    assert team_lists.asked == [("fake-bucket", LIST)]


def test_must_refuse_when_any_channel_in_the_run_is_inside_its_window(tmp_path):
    later = f"{OTHER}/{int((NOTICE_AT + timedelta(days=3)).timestamp())}.000100"
    config = team_config(
        tmp_path, slack_notices=(("example-channel", NOTICED), ("other-channel", later))
    )
    with pytest.raises(SlackExportError, match="#other-channel's opt-out window"):
        start(config, DAY_7, [f"example-channel={CHANNEL}", f"other-channel={OTHER}"])


def test_a_channel_set_to_none_has_no_window(tmp_path):
    report = start(team_config(tmp_path), NOTICE_AT, [f"other-channel={OTHER}"])
    assert "Export run" in report


# --- item 11: no list, no export ----------------------------------------------------------------


def test_must_refuse_when_there_is_no_list(tmp_path, team_lists):
    team_lists.lists = {}
    config = team_config(tmp_path)
    with pytest.raises(SlackExportError, match="isn't in the store yet"):
        start(config, DAY_7)
    assert not run_path(config.data_dir).exists()
    assert not team_opted_out_path(config.data_dir).exists()  # never saved as "nobody"


def test_must_refuse_when_the_list_cant_be_fetched(tmp_path, team_lists):
    team_lists.error = StoreError("Google turned down the store key. Nothing was sent.")
    with pytest.raises(SlackExportError, match="Couldn't fetch your team's opt-out list") as e:
        start(team_config(tmp_path), DAY_7)
    assert "sent" not in str(e.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"format": "diffdata-opted-out", "version": 1, "team": "fake-team"}).encode(),
        json.dumps(
            {"format": "x", "version": 1, "team": "fake-team", "slack_opted_out": []}
        ).encode(),
        json.dumps(
            {
                "format": "diffdata-opted-out",
                "version": 1,
                "team": "fake-team",
                "slack_opted_out": ["not-an-id"],
            }
        ).encode(),  # fmt: skip
        json.dumps(
            {
                "format": "diffdata-opted-out",
                "version": 1,
                "team": "fake-team",
                "slack_opted_out": ["UFAKE00001", "UFAKE00001"],
            }
        ).encode(),  # fmt: skip
    ],
    ids=["text", "list", "no-ids", "format", "bad-id", "repeat"],
)
def test_must_refuse_an_unreadable_list(tmp_path, team_lists, raw):
    team_lists.lists = {LIST: raw}
    with pytest.raises(SlackExportError, match="isn't one diffdata wrote") as e:
        start(team_config(tmp_path), DAY_7)
    assert "not-an-id" not in str(e.value)  # never quoted


def test_must_refuse_another_teams_list(tmp_path, team_lists):
    team_lists.lists = {LIST: team_lists.body("other-team", ["UFAKE00001"])}
    with pytest.raises(SlackExportError, match="for another team"):
        start(team_config(tmp_path), DAY_7)


def test_must_refuse_a_list_written_before_the_window_closed(tmp_path, team_lists):
    team_lists.written = NOTICE_AT + timedelta(days=6)
    with pytest.raises(SlackExportError, match="written before a channel's 7 days ended"):
        start(team_config(tmp_path), DAY_7)


def test_must_refuse_half_a_team_folder(tmp_path):
    with pytest.raises(SlackExportError, match="must both be set"):
        start(team_config(tmp_path, store_folder=""), DAY_7)


def test_must_refuse_without_the_teams_key(tmp_path):
    config = team_config(tmp_path)
    (config.path.parent / "store-key.json").unlink()
    with pytest.raises(SlackExportError, match="no store key.*needed to fetch"):
        start(config, DAY_7)


def test_must_refuse_a_notice_with_no_team_folder(tmp_path, team_lists):
    config = team_config(tmp_path, store_bucket="", store_folder="")
    with pytest.raises(SlackExportError, match="has a notice, so your team's opt-out list"):
        start(config, DAY_7)
    assert team_lists.asked == [] and not run_path(config.data_dir).exists()


def test_without_a_team_folder_or_a_notice_only_slack_opted_out_applies(tmp_path, team_lists):
    config = team_config(tmp_path, store_bucket="", store_folder="")
    report = start(config, DAY_7, [f"other-channel={OTHER}"])
    assert "no team opt-out list was fetched" in report
    assert team_lists.asked == []


def test_the_window_closes_to_the_microsecond(tmp_path):
    closes = NOTICE_AT + timedelta(days=7, microseconds=100)
    with pytest.raises(SlackExportError, match="window is still open"):
        start(team_config(tmp_path), closes - timedelta(microseconds=1))
    assert "Export run" in start(team_config(tmp_path), closes)


def test_must_refuse_a_list_written_a_moment_before_the_window_closed(tmp_path, team_lists):
    team_lists.written = NOTICE_AT + timedelta(days=7, microseconds=99)
    with pytest.raises(SlackExportError, match="written before"):
        start(team_config(tmp_path), DAY_7)


def test_must_refuse_a_repeated_channel_hiding_anothers_window(tmp_path):
    later = f"{OTHER}/{int((NOTICE_AT + timedelta(days=3)).timestamp())}.000100"
    config = team_config(
        tmp_path,
        slack_channels=(),
        slack_channel_ids=(CHANNEL, OTHER),
        slack_notices=((CHANNEL, NOTICED), (OTHER, later)),
    )
    with pytest.raises(SlackExportError, match="Channel 2 given to begin's opt-out window"):
        start(config, DAY_7, [CHANNEL, OTHER, CHANNEL])


def test_must_refuse_a_linked_slack_folder(tmp_path):
    config = team_config(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config.data_dir.mkdir()
    (config.data_dir / "slack").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SlackExportError, match="is a link"):
        start(config, DAY_7)
    assert list(elsewhere.iterdir()) == []


# --- item 11: the save hook, the build and the push need the fetched list ------------------------


def test_must_refuse_saving_building_or_pushing_without_the_fetched_list(
    tmp_path, slack_hook, slack_response
):
    from diffdata.discover.slack_export import build
    from diffdata.discover.slack_inbox import save
    from diffdata.store.push import PushError, push

    config = team_config(tmp_path, store_backend="gcs", store_producer="p", store_project="p")
    start(config, DAY_7)
    team_opted_out_path(config.data_dir).unlink()  # gone after the start
    payload = slack_hook(
        "slack_read_channel", {"channel_id": CHANNEL}, slack_response("read_channel.txt")
    )
    assert save(config, payload, now=DAY_7 + timedelta(minutes=1)) == 2
    assert not any((config.data_dir / "slack" / "inbox").glob("*/*"))
    with pytest.raises(SlackExportError, match="hasn't been fetched.*nothing was built"):
        build(config)
    with pytest.raises(PushError, match="hasn't been fetched.*nothing was sent"):
        push(config)
    # A list fetched for another team's folder doesn't count either.
    team_optouts.save_list(config.data_dir, "fake-bucket", "other-team", [])
    with pytest.raises(SlackExportError, match="hasn't been fetched"):
        build(config)


def test_a_fetched_id_is_left_out_of_the_build(tmp_path, team_lists, slack_run, slack_response):
    from conftest import team_store

    from diffdata.discover.slack_export import build

    path = team_store(tmp_path)["path"]
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[sources]\n'
        'slack_channels = ["example-channel"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f'slack_notices = {{ "example-channel" = "https://example.slack.com/archives/{CHANNEL}/'
        f'p{int(NOTICE_AT.timestamp())}000100" }}\n\n'
        '[store]\nbucket = "fake-bucket"\nfolder = "fake-team"\n',
        encoding="utf-8",
    )
    team_lists.lists = {LIST: team_lists.body("fake-team", ["U00000002"])}
    read = slack_response("read_channel.txt")
    slack_run(load_config(path), DAY_7, [("slack_read_channel", {"channel_id": CHANNEL}, read)])
    report = build(load_config(path))
    assert "1 message(s) left out, because their authors are listed in slack_opted_out" in report
    left_out = json.loads((tmp_path / "data" / "slack" / "left_out.json").read_text("utf-8"))
    assert "U00000002" in json.dumps(left_out)


# --- item 12: merged, never dropped ---------------------------------------------------------------


def write_config(tmp_path: Path, opted_out: list[str]) -> Path:
    home = tmp_path / "fake-diffdata"
    home.mkdir(exist_ok=True)
    path = home / "config.toml"
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[sources]\n'
        f'slack_channels = ["example-channel"]\nslack_opted_out = {json.dumps(opted_out)}\n',
        encoding="utf-8",
    )
    return path


def test_the_fetched_list_adds_to_slack_opted_out(tmp_path, team_lists):
    path = write_config(tmp_path, ["UFAKE00001"])
    team_lists.lists = {LIST: team_lists.body("fake-team", ["UFAKE00002", "UFAKE00001"])}
    start(team_config(tmp_path), DAY_7)
    assert load_config(path).slack_opted_out == ("UFAKE00001", "UFAKE00002")


def test_must_refuse_dropping_anyone_a_later_list_leaves_out(tmp_path, team_lists):
    path = write_config(tmp_path, ["UFAKE00001"])
    team_lists.lists = {LIST: team_lists.body("fake-team", ["UFAKE00002"])}
    start(team_config(tmp_path), DAY_7)
    team_lists.lists = {LIST: team_lists.body("fake-team", ["UFAKE00003"])}
    report = start(team_config(tmp_path), DAY_7 + timedelta(days=1))
    assert "1 person(s) on it, 1 new" in report
    assert load_config(path).slack_opted_out == ("UFAKE00001", "UFAKE00002", "UFAKE00003")


@pytest.mark.parametrize("text", ["{", "[]", '{"format": "diffdata-team-opted-out"}'])
def test_must_refuse_a_damaged_saved_list(tmp_path, text):
    path = write_config(tmp_path, [])
    saved = team_opted_out_path(tmp_path / "data")
    saved.parent.mkdir(parents=True)
    saved.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="damaged"):
        load_config(path)


# --- the store's read ------------------------------------------------------------------------


def test_the_store_read_uses_the_read_only_scope_and_that_version(monkeypatch):
    from google.api_core import exceptions as api

    from diffdata.store import gcs

    seen = {}

    class Credentials:
        def refresh(self, request):
            pass

    class Blob:
        generation = 7
        time_created = DAY_7

        def download_as_bytes(self, if_generation_match):
            seen["generation"] = if_generation_match
            return b"{}"

    class Bucket:
        def __init__(self, blobs):
            self.blobs = blobs

        def get_blob(self, name):
            seen["name"] = name
            found = self.blobs.get(name)
            if isinstance(found, Exception):
                raise found
            return found

    class Rewritten(Blob):
        def download_as_bytes(self, if_generation_match):
            raise api.PreconditionFailed("changed")

    blobs = {LIST: Blob(), "denied": api.Forbidden("no"), "rewritten": Rewritten()}

    def from_info(key, scopes):
        seen["scopes"] = scopes
        return Credentials()

    class Client:
        def __init__(self, **kwargs):
            pass

        def bucket(self, name):
            return Bucket(blobs)

    monkeypatch.setattr(gcs.service_account.Credentials, "from_service_account_info", from_info)
    monkeypatch.setattr(gcs.storage, "Client", Client)
    assert gcs.fetch("fake-bucket", KEY, LIST) == (b"{}", DAY_7)
    assert seen["scopes"] == ["https://www.googleapis.com/auth/devstorage.read_only"]
    assert seen["generation"] == 7
    assert gcs.fetch("fake-bucket", KEY, "missing") is None
    with pytest.raises(StoreError, match="can't read its _config folder"):
        gcs.fetch("fake-bucket", KEY, "denied")
    with pytest.raises(StoreError, match="changed while it was read"):
        gcs.fetch("fake-bucket", KEY, "rewritten")


# --- item 9: diffdata admin optouts ------------------------------------------------------------


class FakeGcloud:
    """The gcloud commands optouts runs, against one made-up team folder."""

    def __init__(self, config_folder=True, existing: bytes | None = None, generation="41"):
        self.calls: list[list[str]] = []
        self.config_folder = config_folder
        self.existing = existing
        self.generation = generation
        self.written: dict | None = None
        self.cp_error: str | None = None

    def __call__(self, args):
        self.calls.append(args)
        assert args[-1] == "--quiet", args

        def answer(code=0, out="", err=""):
            return subprocess.CompletedProcess([], code, stdout=out, stderr=err)

        if args[:3] == ["storage", "managed-folders", "describe"]:
            assert args[3] == "gs://fake-bucket/fake-team/_config/"
            return answer(out="{}") if self.config_folder else answer(1, err="404 Not Found")
        if args[:3] == ["storage", "objects", "describe"]:
            assert args[3] == URL
            if self.existing is None:
                return answer(1, err="ERROR: 404 does not exist")
            return answer(out=json.dumps({"generation": self.generation}))
        if args[:2] == ["storage", "cat"]:
            assert args[2] == f"{URL}#{self.generation}"
            return answer(out=self.existing.decode())
        if args[:2] == ["storage", "cp"]:
            assert args[3] == URL
            path = Path(args[2])
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            self.written = json.loads(path.read_text(encoding="utf-8"))
            if self.cp_error:
                self.written = None
                return answer(1, err=self.cp_error)
            return answer()
        raise AssertionError(f"optouts ran a command the fake doesn't know: {args}")


def run_optouts(gcloud, lines, yes=True, team="fake-team", bucket="fake-bucket"):
    said: list[str] = []
    typed = iter(lines)
    asked: list[str] = []

    def ask_yes(question):
        asked.append(question)
        return yes

    code = admin.optouts(
        team, bucket, ask=lambda _: next(typed), ask_yes=ask_yes, say=said.append, gcloud=gcloud
    )
    return code, "\n".join(said), asked


@POSIX
def test_must_refuse_writing_a_list_without_a_yes():
    gcloud = FakeGcloud()
    code, said, asked = run_optouts(gcloud, ["UFAKE00001", ""], yes=False)
    assert code == 1 and gcloud.written is None
    assert "Team fake-team" in said and "0 already on the list, 1 to add, 1 in all" in said
    assert "add UFAKE00001" in said
    assert asked and "looked at the form's answers" in asked[0]
    assert not any(c[:2] == ["storage", "cp"] for c in gcloud.calls)


@POSIX
def test_a_new_list_is_written_only_if_there_is_none_yet():
    gcloud = FakeGcloud()
    code, said, _ = run_optouts(gcloud, ["not an id", "UFAKE00001", "UFAKE00001", "WFAKE00002", ""])
    assert code == 0
    assert gcloud.written == {
        "format": "diffdata-opted-out",
        "version": 1,
        "team": "fake-team",
        "slack_opted_out": ["UFAKE00001", "WFAKE00002"],
    }
    cp = next(c for c in gcloud.calls if c[:2] == ["storage", "cp"])
    assert "--if-generation-match=0" in cp
    assert "isn't a Slack user ID" in said and "in the list already" in said
    # What admin writes is what the export reads.
    assert team_optouts.parse_list(json.dumps(gcloud.written).encode(), "fake-team") == [
        "UFAKE00001",
        "WFAKE00002",
    ]


@POSIX
def test_nobody_opted_out_is_written_only_when_typed():
    gcloud = FakeGcloud()
    code, said, _ = run_optouts(gcloud, ["", "none"])
    assert code == 0 and gcloud.written["slack_opted_out"] == []
    assert "Type the IDs, or none." in said


@POSIX
def test_must_refuse_shortening_a_list_already_there():
    existing = team_optouts_body(["UFAKE00001"])
    gcloud = FakeGcloud(existing=existing)
    code, said, _ = run_optouts(gcloud, ["UFAKE00002", ""])
    assert code == 0
    assert gcloud.written["slack_opted_out"] == ["UFAKE00001", "UFAKE00002"]
    assert "1 already on the list, 1 to add, 2 in all" in said
    cp = next(c for c in gcloud.calls if c[:2] == ["storage", "cp"])
    assert "--if-generation-match=41" in cp


@POSIX
def test_must_refuse_when_the_list_changed_while_typing():
    gcloud = FakeGcloud()
    gcloud.cp_error = "ERROR: HTTPError 412: At least one of the pre-conditions did not hold."
    with pytest.raises(AdminError, match="changed while you typed"):
        run_optouts(gcloud, ["UFAKE00001", ""])


@POSIX
def test_must_refuse_a_list_there_that_isnt_this_teams():
    gcloud = FakeGcloud(existing=team_optouts_body(["UFAKE00001"], team="other-team"))
    with pytest.raises(AdminError, match="isn't one admin wrote for fake-team"):
        run_optouts(gcloud, [])


@POSIX
def test_must_refuse_a_team_with_no_config_folder():
    gcloud = FakeGcloud(config_folder=False)
    with pytest.raises(AdminError, match="Run add-team for this team first"):
        run_optouts(gcloud, [])
    assert len(gcloud.calls) == 1


@POSIX
@pytest.mark.parametrize(("team", "bucket"), [("fake/team", "fake-bucket"), ("t", "Bad Bucket")])
def test_must_refuse_names_push_would_refuse(team, bucket):
    gcloud = FakeGcloud()
    with pytest.raises(AdminError, match="Nothing was changed"):
        run_optouts(gcloud, [], team=team, bucket=bucket)
    assert gcloud.calls == []


@POSIX
def test_the_yes_drops_anything_pasted_ahead_of_it(monkeypatch):
    flushed = []
    monkeypatch.setattr(cli.review, "in_terminal", lambda *a: True)
    monkeypatch.setattr(cli.termios, "tcflush", lambda *a: flushed.append(a))

    class Prompter:
        ask = staticmethod(lambda q: "y")

        def yes(self, question):
            return True

    monkeypatch.setattr(cli.producer_setup, "Prompter", Prompter)
    monkeypatch.setattr(admin, "optouts", lambda *a, ask, ask_yes: 0 if ask_yes("q?") else 1)
    assert cli.main(["admin", "optouts", "fake-team", "--bucket", "fake-bucket"]) == 0
    assert len(flushed) == 1


def test_admin_optouts_refuses_outside_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *a: False)
    called = []
    monkeypatch.setattr(admin, "optouts", lambda *a, **k: called.append(1))
    assert cli.main(["admin", "optouts", "fake-team", "--bucket", "fake-bucket"]) == 1
    assert "your own terminal" in capsys.readouterr().err and called == []


def team_optouts_body(ids, team="fake-team") -> bytes:
    data = {"format": "diffdata-opted-out", "version": 1, "team": team, "slack_opted_out": ids}
    return json.dumps(data).encode()
