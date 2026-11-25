"""`diffdata admin add-team` (#132 PR 2). One test or more per must-refuse item 1 to 5.

`gcloud` is faked: FakeGcloud keeps a made-up project's accounts, keys and managed folders,
answers as gcloud would, and records every command, so the tests check the exact commands and
roles. Nothing here runs gcloud or reaches Google. The key is made up, with a marker that must
never show in what add-team prints. Every name and address is an example.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common import setup as producer_setup
from diffdata.store import admin
from diffdata.store.admin import AdminError, Team

# Google's role names, written out here so a change to admin.py's own constants fails a test.
CREATOR = "roles/storage.objectCreator"
VIEWER = "roles/storage.objectViewer"
OURS = "diffdata upload: example-bucket/team-a"
MARKER = "MADE-UP-ADMIN-KEY-MARKER"
GCP = "example-gcp-project"
EMAIL = f"dd-team-a@{GCP}.iam.gserviceaccount.com"
UPLOAD = f"serviceAccount:{EMAIL}"
ROOT = "gs://example-bucket/team-a/"
FORM = "https://forms.example.com/d/e/FAKE-FORM/viewform?usp=pp_url&entry.100="
CLIENT = {"installed": {"client_id": f"{MARKER}.apps.example", "client_secret": MARKER}}
POSIX = pytest.mark.skipif(os.name == "nt", reason="file modes are for macOS and Linux")


def fake_key(email=EMAIL) -> dict:
    return {
        "type": "service_account",
        "private_key": f"-----BEGIN PRIVATE KEY-----\n{MARKER}\n-----END PRIVATE KEY-----\n",
        "private_key_id": "FAKE-KEY-ID",
        "client_email": email,
        "project_id": GCP,
    }


class FakeGcloud:
    """A made-up Cloud project, answering the gcloud commands add-team runs."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.accounts: dict[str, str] = {}  # email -> display name
        self.keys: dict[str, int] = {}
        self.folders: dict[str, list[dict]] = {}  # url -> bindings
        self.fail: dict[str, str] = {}  # a command word -> stderr to fail with

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        assert args[-1] == "--quiet", args
        words = [a for a in args[:-1] if not a.startswith("--")]
        flags = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
        for word, stderr in self.fail.items():
            if word in " ".join(args):
                return self.answer(1, err=stderr)
        if words[:2] == ["iam", "service-accounts"]:
            return self.accounts_command(words[2:], flags)
        if words[:2] == ["storage", "managed-folders"]:
            return self.folders_command(words[2], words[3], flags)
        raise AssertionError(f"add-team ran a command the fake doesn't know: {args}")

    @staticmethod
    def answer(code=0, out="", err=""):
        return subprocess.CompletedProcess([], code, stdout=out, stderr=err)

    def accounts_command(self, words, flags):
        if words[0] == "list":
            email = flags["filter"].removeprefix("email=")
            found = [{"email": e, "displayName": n} for e, n in self.accounts.items() if e == email]
            return self.answer(out=json.dumps(found))
        if words[0] == "create":
            email = f"{words[1]}@{flags['project']}.iam.gserviceaccount.com"
            self.accounts[email] = flags["display-name"]
            return self.answer()
        if words[:2] == ["keys", "list"]:
            n = self.keys.get(flags["iam-account"], 0)
            return self.answer(out=json.dumps([{"name": f"key-{i}"} for i in range(n)]))
        if words[:2] == ["keys", "delete"]:
            assert words[2] == "FAKE-KEY-ID"
            self.keys[flags["iam-account"]] -= 1
            return self.answer()
        if words[:2] == ["keys", "create"]:
            email = flags["iam-account"]
            self.keys[email] = self.keys.get(email, 0) + 1
            Path(words[2]).write_text(json.dumps(fake_key(email)), encoding="utf-8")
            return self.answer(err=f"created key [FAKE-KEY-ID] of type [json] as [{words[2]}]")
        raise AssertionError(words)

    def folders_command(self, verb, url, flags):
        if verb == "list":
            bucket, prefix = url.removeprefix("gs://").split("/", 1)
            found = [
                {"bucket": bucket, "name": f.removeprefix(f"gs://{bucket}/")}
                for f in self.folders
                if f.startswith(url)
            ]
            return self.answer(out=json.dumps(found))
        if verb == "describe":
            if url not in self.folders:
                return self.answer(1, err="ERROR: NotFoundError: 404 The managed folder ...")
            return self.answer(out=json.dumps({"name": url}))
        if verb == "create":
            assert url not in self.folders, "a managed folder made twice"
            parent = url.rstrip("/").rsplit("/", 1)[0] + "/"
            assert parent == "gs://example-bucket/" or parent in self.folders, "parent first"
            self.folders[url] = []
            return self.answer()
        if verb == "get-iam-policy":
            return self.answer(out=json.dumps({"bindings": self.folders[url]}))
        if verb == "add-iam-policy-binding":
            member, role = flags["member"], flags["role"]
            if member.startswith("serviceAccount:") and member[15:] not in self.accounts:
                return self.answer(1, err="ERROR: Service account ... does not exist.")
            bindings = self.folders[url]
            found = next((b for b in bindings if b["role"] == role), None)
            if found is None:
                bindings.append({"role": role, "members": [member]})
            elif member not in found["members"]:
                found["members"].append(member)
            return self.answer()
        raise AssertionError(verb)

    def changes(self):
        """Every command that changed something: anything but describe, list and get."""
        return [c for c in self.calls if not ({"describe", "list", "get-iam-policy"} & set(c))]


@pytest.fixture
def out(tmp_path):
    """A plain folder for team files, outside any repository (tmp_path isn't in one)."""
    return tmp_path / "teams"


def team(out: Path, **changes) -> Team:
    values = dict(
        name="team-a",
        bucket="example-bucket",
        project="example-project",
        gcp_project=GCP,
        producers=("producer-one@example.com", "producer-two@example.org"),
        out=out,
        optout_form=FORM,
        google_client=CLIENT,
    )
    values.update(changes)
    return Team(**values)


def run(chosen: Team, fake=None, yes=True) -> tuple[int, FakeGcloud, list[str], list[str]]:
    fake = fake or FakeGcloud()
    asked, said = [], []

    def ask_yes(question):
        asked.append(question)
        return yes

    code = admin.add_team(chosen, ask_yes=ask_yes, say=said.append, gcloud=fake, sleep=lambda s: 0)
    return code, fake, asked, said


EXPECTED_CHANGES = [
    ["iam", "service-accounts", "create", "dd-team-a", f"--project={GCP}",
     f"--display-name={OURS}", "--quiet"],
    ["storage", "managed-folders", "create", ROOT, "--quiet"],
    ["storage", "managed-folders", "create", f"{ROOT}approved/", "--quiet"],
    ["storage", "managed-folders", "create", f"{ROOT}not-approved/", "--quiet"],
    ["storage", "managed-folders", "create", f"{ROOT}_config/", "--quiet"],
    ["storage", "managed-folders", "add-iam-policy-binding", ROOT,
     "--member=user:producer-one@example.com", f"--role={VIEWER}", "--quiet"],
    ["storage", "managed-folders", "add-iam-policy-binding", ROOT,
     "--member=user:producer-two@example.org", f"--role={VIEWER}", "--quiet"],
    ["storage", "managed-folders", "add-iam-policy-binding", f"{ROOT}approved/",
     f"--member={UPLOAD}", f"--role={CREATOR}", "--quiet"],
    ["storage", "managed-folders", "add-iam-policy-binding", f"{ROOT}not-approved/",
     f"--member={UPLOAD}", f"--role={CREATOR}", "--quiet"],
    ["storage", "managed-folders", "add-iam-policy-binding", f"{ROOT}_config/",
     f"--member={UPLOAD}", f"--role={VIEWER}", "--quiet"],
]  # fmt: skip


# --- a whole run --------------------------------------------------------------------------


def test_a_run_makes_exactly_these_changes_then_the_key(out):
    code, fake, asked, said = run(team(out))
    assert code == 0
    assert asked == ["Make these changes?"]
    changes = fake.changes()
    assert changes[:-1] == EXPECTED_CHANGES
    key_create = changes[-1]
    assert key_create[:4] == ["iam", "service-accounts", "keys", "create"]
    assert key_create[5:] == [f"--iam-account={EMAIL}", f"--project={GCP}", "--quiet"]
    assert not Path(key_create[4]).exists()  # the key's temporary file is gone
    assert not Path(key_create[4]).parent.exists()


def test_the_team_file_is_one_setup_reads(out):
    run(team(out))
    read = producer_setup.read_team_file(out / "team-a.diffdata-team.json")
    assert read.values == {
        "bucket": "example-bucket",
        "folder": "team-a",
        "project": "example-project",
    }
    assert json.loads(read.store_key)["client_email"] == EMAIL
    assert json.loads(read.google_client) == CLIENT
    assert sorted(p.name for p in out.iterdir()) == ["team-a.diffdata-team.json"]


def test_the_google_client_may_be_left_out(out):
    run(team(out, google_client=None))
    data = json.loads((out / "team-a.diffdata-team.json").read_text(encoding="utf-8"))
    assert set(data) == {"format", "version", "bucket", "folder", "project", "store_key"}


def test_it_prints_the_teams_opt_out_link_and_the_notice(out):
    _, _, _, said = run(team(out, name="team_b"))
    text = "\n".join(said)
    assert f"Opt-out link for team_b: {FORM}team_b" in text
    assert text.count(f"{FORM}team_b") == 2  # the line, and inside the notice
    assert "Placeholder wording" in text


def test_without_a_form_the_notice_has_a_gap_to_fill(out):
    _, _, _, said = run(team(out, optout_form=None))
    text = "\n".join(said)
    assert "<the team's opt-out link>" in text and "No --optout-form given" in text


def test_a_no_changes_nothing(out):
    code, fake, _, said = run(team(out), yes=False)
    assert code == 1
    assert fake.changes() == []
    assert said[-1] == admin.NOTHING_CHANGED
    assert not out.exists()


def test_a_new_account_not_yet_known_to_storage_is_granted_after_a_wait(out):
    fake = FakeGcloud()
    real = fake.folders_command
    misses = {"left": 2}

    def slow(verb, url, flags):
        if verb == "add-iam-policy-binding" and flags["member"] == UPLOAD and misses["left"]:
            misses["left"] -= 1
            return fake.answer(1, err="ERROR: Service account ... does not exist.")
        return real(verb, url, flags)

    fake.folders_command = slow
    code, *_ = run(team(out), fake=fake)
    assert code == 0


def test_a_rerun_after_a_stop_before_the_key_finishes_the_job(out):
    fake = FakeGcloud()
    fake.fail["keys create"] = "ERROR: quota"
    with pytest.raises(AdminError) as stopped:
        run(team(out), fake=fake)
    assert "Changed so far" in str(stopped.value) and f"made {ROOT}_config/" in str(stopped.value)
    del fake.fail["keys create"]
    fake.calls.clear()
    code, *_ = run(team(out), fake=fake)
    assert code == 0
    assert not any("create" in c and "managed-folders" in c for c in fake.calls)
    assert fake.keys[EMAIL] == 1


# --- must refuse 1: no grant but these, no project or bucket policy -----------------------


def test_must_refuse_1_every_policy_change_is_one_of_the_allowed_grants(out):
    _, fake, _, _ = run(team(out))
    allowed = {
        (f"{ROOT}approved/", UPLOAD, CREATOR),
        (f"{ROOT}not-approved/", UPLOAD, CREATOR),
        (f"{ROOT}_config/", UPLOAD, VIEWER),
        (ROOT, "user:producer-one@example.com", VIEWER),
        (ROOT, "user:producer-two@example.org", VIEWER),
    }
    grants = set()
    for call in fake.calls:
        text = " ".join(call)
        assert "set-iam-policy" not in text and "remove-iam-policy-binding" not in text
        assert "projects" not in call and "buckets" not in call and "organizations" not in call
        if "add-iam-policy-binding" in call:
            assert call[:3] == ["storage", "managed-folders", "add-iam-policy-binding"]
            flags = dict(a[2:].split("=", 1) for a in call if "=" in a)
            grants.add((call[3], flags["member"], flags["role"]))
    assert grants == allowed


def test_must_refuse_1_the_upload_account_gets_no_creator_on_the_whole_folder(out):
    # A grant on a folder covers the folders inside it: Creator on the team's folder would let
    # the key add _config/opted-out.json itself.
    _, fake, _, _ = run(team(out))
    on_root = [c for c in fake.calls if "add-iam-policy-binding" in c and c[3] == ROOT]
    assert on_root and all(f"--member={UPLOAD}" not in c for c in on_root)
    assert all(f"--role={CREATOR}" not in c for c in on_root)


def test_must_refuse_1_producers_get_viewer_only(out):
    _, fake, _, _ = run(team(out))
    for call in fake.calls:
        if any(a.startswith("--member=user:") for a in call):
            assert f"--role={VIEWER}" in call and call[3] == ROOT


# --- must refuse 2: never another team's folder ------------------------------------------


@pytest.mark.parametrize("folder", [ROOT, f"{ROOT}approved/", f"{ROOT}_config/"])
def test_must_refuse_2_a_folder_with_someone_else_in_it_stops_everything(out, folder):
    fake = FakeGcloud()
    fake.folders[ROOT] = []
    fake.folders[folder] = [{"role": VIEWER, "members": ["user:someone-else@example.net"]}]
    with pytest.raises(AdminError, match="another team's folder"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_a_folder_where_ours_has_a_bigger_role_stops(out):
    fake = FakeGcloud()
    fake.folders[ROOT] = [{"role": CREATOR, "members": [UPLOAD]}]
    with pytest.raises(AdminError, match="another team's folder"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_an_account_of_that_name_not_made_for_this_team_stops(out):
    fake = FakeGcloud()
    fake.accounts[EMAIL] = "someone else's account"
    with pytest.raises(AdminError, match="Choose another team name"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_an_account_that_has_a_key_already_stops(out):
    fake = FakeGcloud()
    fake.accounts[EMAIL] = OURS
    fake.keys[EMAIL] = 1
    with pytest.raises(AdminError, match="has a key already"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_a_check_that_fails_for_another_reason_stops(out):
    fake = FakeGcloud()
    fake.fail["managed-folders describe"] = "ERROR: 403 permission denied"
    with pytest.raises(AdminError, match="Couldn't check"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_the_bucket_itself_is_never_named_alone(out):
    _, fake, _, _ = run(team(out))
    for call in fake.calls:
        assert "gs://example-bucket" not in call and "gs://example-bucket/" not in call


# --- must refuse 3: never print the key --------------------------------------------------


def test_must_refuse_3_the_key_is_never_printed(out, capsys):
    _, _, _, said = run(team(out))
    text = "\n".join(said) + capsys.readouterr().out
    assert MARKER not in text and "FAKE-KEY-ID" not in text and "PRIVATE KEY" not in text
    assert any(line.startswith("Done: 1 account made, 4 folders made, 5 grants") for line in said)
    assert f"Team file: {out / 'team-a.diffdata-team.json'}" in said


def test_must_refuse_3_a_stop_after_the_key_doesnt_print_it(out):
    fake = FakeGcloud()
    out.mkdir()
    (out / "team-a.diffdata-team.json").mkdir()  # appears while gcloud runs, in the test below
    with pytest.raises(AdminError) as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert fake.changes() == []  # caught before any change


def test_must_refuse_3_a_team_file_that_appears_while_gcloud_runs_isnt_replaced(out):
    fake = FakeGcloud()
    real = fake.accounts_command

    def appear(words, flags):
        if words[:2] == ["keys", "create"]:
            out.mkdir(exist_ok=True)
            (out / "team-a.diffdata-team.json").write_text("someone's file", encoding="utf-8")
        return real(words, flags)

    fake.accounts_command = appear
    with pytest.raises(AdminError) as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert "deleted the new key again" in str(stopped.value)
    assert fake.keys[EMAIL] == 0  # the key no team file holds is gone, so a rerun can finish
    assert (out / "team-a.diffdata-team.json").read_text(encoding="utf-8") == "someone's file"
    assert [p.name for p in out.iterdir()] == ["team-a.diffdata-team.json"]


def test_must_refuse_3_a_key_that_isnt_a_key_stops_without_showing_it(out):
    fake = FakeGcloud()
    real = fake.accounts_command

    def bad(words, flags):
        result = real(words, flags)
        if words[:2] == ["keys", "create"]:
            Path(words[2]).write_text(f'{{"type": "user", "secret": "{MARKER}"}}')
        return result

    fake.accounts_command = bad
    with pytest.raises(AdminError, match="isn't a service account key") as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert not out.exists() or not any(out.iterdir())


# --- must refuse 4: only outside repos and synced folders, readable only by you ------------


def test_must_refuse_4_not_inside_a_git_repository(tmp_path):
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="git repository or a synced folder"):
        run(team(repo / "teams"), fake=fake)
    assert fake.calls == []


@pytest.mark.parametrize("synced", ["Dropbox", "Google Drive", "OneDrive - Example"])
def test_must_refuse_4_not_inside_a_synced_folder(tmp_path, synced):
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="git repository or a synced folder"):
        run(team(tmp_path / synced / "teams"), fake=fake)
    assert fake.calls == []


def test_must_refuse_4_not_through_a_link_into_a_repository(tmp_path):
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    link = tmp_path / "teams-link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(AdminError, match="git repository or a synced folder"):
        run(team(link))


def test_must_refuse_4_never_replaces_a_team_file(out):
    out.mkdir()
    (out / "team-a.diffdata-team.json").write_text("an older one", encoding="utf-8")
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="never replaces one"):
        run(team(out), fake=fake)
    assert fake.calls == []
    assert (out / "team-a.diffdata-team.json").read_text(encoding="utf-8") == "an older one"


@POSIX
def test_must_refuse_4_the_team_file_is_readable_only_by_you(out):
    run(team(out))
    mode = stat.S_IMODE((out / "team-a.diffdata-team.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


# --- must refuse 5: names push would refuse -------------------------------------------------


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"name": "team/a"}, "team name"),
        ({"name": "team a"}, "team name"),
        ({"name": "../team-a"}, "team name"),
        ({"name": ""}, "team name"),
        ({"project": "example/project"}, "project"),
        ({"project": "example project"}, "project"),
        ({"bucket": "Example_Bucket"}, "bucket"),
        ({"bucket": "google-bucket"}, "bucket"),
    ],
)
def test_must_refuse_5_names_push_would_refuse(out, changes, words):
    fake = FakeGcloud()
    with pytest.raises(AdminError, match=words):
        run(team(out, **changes), fake=fake)
    assert fake.calls == []


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"name": "a"}, "account name"),  # too short for Google's account names
        ({"name": "team-a-with-a-very-long-name-x"}, "account name"),
        ({"name": "team-"}, "account name"),
        ({"gcp_project": "Example"}, "gcp-project"),
        ({"producers": ()}, "--producer"),
        ({"producers": ("not an email",)}, "--producer"),
        ({"producers": ("a@example.com,b@example.com",)}, "--producer"),
        ({"optout_form": "http://forms.example.com/viewform?entry.1="}, "--optout-form"),
        ({"optout_form": "https://forms.example.com/viewform"}, "--optout-form"),
        ({"google_client": {"web": {}}}, "--google-client"),
    ],
)
def test_other_values_add_team_refuses(out, changes, words):
    fake = FakeGcloud()
    with pytest.raises(AdminError, match=words):
        run(team(out, **changes), fake=fake)
    assert fake.calls == []


# --- the command line --------------------------------------------------------------------


def test_the_command_refuses_outside_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: False)
    monkeypatch.setattr(admin, "run_gcloud", lambda args: pytest.fail("gcloud ran"))
    code = cli.main(
        ["admin", "add-team", "team-a", "--bucket", "example-bucket", "--project",
         "example-project", "--gcp-project", GCP, "--producer", "producer-one@example.com"]
    )  # fmt: skip
    assert code == 1
    assert "your own terminal" in capsys.readouterr().err


def test_the_command_reports_a_refusal_without_a_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    code = cli.main(
        ["admin", "add-team", "team/a", "--bucket", "example-bucket", "--project",
         "example-project", "--gcp-project", GCP, "--producer", "producer-one@example.com",
         "--out", str(tmp_path / "teams")]
    )  # fmt: skip
    assert code == 1
    assert capsys.readouterr().err.startswith("diffdata admin: The team name must be")


# --- Codex round 1 ------------------------------------------------------------------------


def test_an_account_check_that_fails_stops_everything(out):
    # Google can answer "permission denied" for a missing account, so absence comes from a
    # successful list, and any failure stops.
    fake = FakeGcloud()
    fake.fail["service-accounts list"] = "ERROR: (gcloud) PERMISSION_DENIED"
    with pytest.raises(AdminError, match="Couldn't check the upload account"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_an_account_made_for_another_bucket_stops(out):
    fake = FakeGcloud()
    fake.accounts[EMAIL] = "diffdata upload: other-bucket/team-a"
    with pytest.raises(AdminError, match="Choose another team name"):
        run(team(out), fake=fake)
    assert fake.changes() == []


@pytest.mark.parametrize("members", [[], ["user:someone-else@example.net"]])
def test_must_refuse_2_a_deeper_folder_inside_the_team_folder_stops(out, members):
    # It would get the new grants by inheritance.
    fake = FakeGcloud()
    fake.folders[ROOT] = []
    fake.folders[f"{ROOT}approved/"] = []
    fake.folders[f"{ROOT}approved/older/"] = [{"role": VIEWER, "members": members}]
    with pytest.raises(AdminError, match="a folder this run wouldn't make"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_must_refuse_2_a_member_added_while_the_plan_is_shown_stops(out):
    fake = FakeGcloud()
    fake.folders[ROOT] = []

    def sneak(question):
        fake.folders[ROOT].append({"role": VIEWER, "members": ["user:someone-else@example.net"]})
        return True

    with pytest.raises(AdminError, match="another team's folder"):
        admin.add_team(team(out), ask_yes=sneak, say=lambda line: None, gcloud=fake)
    assert fake.changes() == []


def test_must_refuse_3_a_failed_key_step_never_shows_gclouds_message(out):
    fake = FakeGcloud()
    fake.fail["keys create"] = f"ERROR: something went wrong\nprivateKeyData: {MARKER}"
    with pytest.raises(AdminError, match="isn't shown") as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)


def test_must_refuse_3_gcloud_runs_with_its_logs_off(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("CLOUDSDK_CORE_LOG_HTTP", "true")
    monkeypatch.setattr(admin.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(admin.subprocess, "run", fake_run)
    admin.run_gcloud(["iam", "service-accounts", "list"])
    assert seen["env"]["CLOUDSDK_CORE_LOG_HTTP"] == "false"
    assert seen["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"
    assert seen["capture_output"] and seen["stdin"] == subprocess.DEVNULL


def test_must_refuse_3_the_key_is_made_inside_the_checked_folder(out):
    _, fake, _, _ = run(team(out))
    key_create = next(
        c for c in fake.calls if c[:4] == ["iam", "service-accounts", "keys", "create"]
    )
    assert Path(key_create[4]).parent.parent == out


def test_must_refuse_4_add_team_refuses_on_windows(out, monkeypatch):
    monkeypatch.setattr(admin, "WINDOWS", True)
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="macOS or Linux"):
        run(team(out), fake=fake)
    assert fake.calls == []
