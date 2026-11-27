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
OURS = "diffdata upload: example-bucket/DD-7KQ4"
MARKER = "MADE-UP-ADMIN-KEY-MARKER"
GCP = "example-gcp-project"
EMAIL = f"dd-dd-7kq4@{GCP}.iam.gserviceaccount.com"
UPLOAD = f"serviceAccount:{EMAIL}"
ROOT = "gs://example-bucket/DD-7KQ4/"
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
        self.objects: list[str] = []  # gs:// addresses of files in the bucket

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
        if words[:2] == ["storage", "ls"]:
            prefix = words[2].removesuffix("**")
            found = [o for o in self.objects if o.startswith(prefix)]
            if not found:
                return self.answer(
                    1, err="ERROR: (gcloud.storage.ls) One or more URLs matched no objects."
                )
            return self.answer(out="\n".join(found))
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
        looks = {"describe", "list", "get-iam-policy", "ls"}
        return [c for c in self.calls if not (looks & set(c))]


@pytest.fixture(autouse=True)
def not_windows(monkeypatch):
    """add-team refuses on Windows. Its other checks are tested there too, as if on macOS."""
    monkeypatch.setattr(admin, "WINDOWS", False)


@pytest.fixture
def out(tmp_path):
    """A plain folder for team files, outside any repository (tmp_path isn't in one)."""
    return tmp_path / "teams"


def team(out: Path, **changes) -> Team:
    values = dict(
        name="DD-7KQ4",
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
    ["iam", "service-accounts", "create", "dd-dd-7kq4", f"--project={GCP}",
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
    read = producer_setup.read_team_file(out / "DD-7KQ4.diffdata-team.json")
    assert read.values == {
        "bucket": "example-bucket",
        "folder": "DD-7KQ4",
        "project": "example-project",
    }
    assert json.loads(read.store_key)["client_email"] == EMAIL
    assert json.loads(read.google_client) == CLIENT
    assert sorted(p.name for p in out.iterdir()) == ["DD-7KQ4.diffdata-team.json"]


def test_the_google_client_may_be_left_out(out):
    run(team(out, google_client=None))
    data = json.loads((out / "DD-7KQ4.diffdata-team.json").read_text(encoding="utf-8"))
    assert set(data) == {"format", "version", "bucket", "folder", "project", "store_key"}


# --- the texts (#150, items 1, 3, 4 and 6; must refuse 19 to 21) -----------------------------

# The agreed texts, copied here from the sign-up procedure doc ("Message texts"), with the one
# line #150's D15 changed, so a change to admin.py's own copy fails a test (must refuse 20).
AGREED_NOTICE = (
    "**Heads up: I've signed this channel up for the SPAR project *Differential Data for "
    "Automated AI Safety Research*. Project code: [code]**\n"
    "\n"
    "The project collects the messages and docs where research decisions actually get made, the "
    "part final papers leave out, to help AI safety research. About the project: "
    "https://docs.google.com/document/d/1ggbdAYohoFG06BrUmxuWczUXqiHAqD3CBWN2z3ShW54/edit\n"
    "\n"
    "- **On [close date] I'll run their tool on this channel.** It leaves out anyone who opted "
    "out, pseudonymises names (replacing them with codes like [Person 3]) and removes contact "
    "details and secrets, and I review everything before it's uploaded.\n"
    "- **The scrubbed data goes to the project's storage** and is shared only with approved "
    "research groups, each for one use: control evaluations and conceptual reasoning at Redwood "
    "Research, and SPAR's study of how research programs run. Some people think conceptual "
    "reasoning work speeds up AI capabilities, so you can leave your data out of that use "
    "alone.\n"
    "- **By [close date], use the opt-out form ([opt-out link]) with project code [code]** to "
    "opt out of everything, or of particular uses. You don't need to give your name or a "
    "reason. After the 7 days, the project team gives my copy of the tool the list of who to "
    "leave out.\n"
    "- **You can change your choices or object any time after that,** with the same form, and "
    "they'll act on it within 14 days.\n"
    "\n"
    "Please keep sensitive or private personal details out of this channel. Questions: ask me, "
    "or spardiffdata＠gmail.com. Privacy notice: "
    "https://docs.google.com/document/d/10lLqAHVoNYXux-fWmzYfOuZj-ywjsMOZwfbTZs-Rvkc/edit"
)
AGREED_MENTOR = (
    "Hi [mentor], a heads-up: I've signed our project channel #[channel] up for the SPAR project "
    "*Differential Data for Automated AI Safety Research*. It collects research discussions, "
    "scrubbed and reviewed by me first, for AI safety research. Nothing is collected before "
    "[close date]. If you'd like your messages and edits left out, or kept out of particular "
    "uses such as conceptual reasoning, use the opt-out form ([opt-out link]) with project code "
    "[code], or just tell me. [If you'd rather the channel weren't included at all, tell me and "
    "I'll withdraw it.] More: About the project "
    "https://docs.google.com/document/d/1ggbdAYohoFG06BrUmxuWczUXqiHAqD3CBWN2z3ShW54/edit, "
    "Privacy notice "
    "https://docs.google.com/document/d/10lLqAHVoNYXux-fWmzYfOuZj-ywjsMOZwfbTZs-Rvkc/edit"
)
LINK = f"{FORM}DD-7KQ4"


def printed(said: list[str], heading: str) -> str:
    """The text printed two lines after the line that starts with `heading`."""
    start = next(i for i, line in enumerate(said) if line.startswith(heading)) + 2
    return said[start]


def test_must_refuse_20_the_notice_is_the_agreed_text_word_for_word(out):
    _, _, _, said = run(team(out))
    notice = printed(said, "Channel notice")
    assert notice.replace(LINK, "[opt-out link]").replace("DD-7KQ4", "[code]") == AGREED_NOTICE
    assert notice.count("DD-7KQ4") == 3  # twice as the code, once at the end of the link
    assert "The project team tells me who to leave out" not in notice  # D15
    assert "＠" in notice and "spardiffdata@" not in notice  # the full-width at, as given


def test_must_refuse_20_the_mentor_message_is_the_agreed_text_word_for_word(out):
    _, _, _, said = run(team(out))
    mentor = printed(said, "Mentor message")
    assert mentor.replace(LINK, "[opt-out link]").replace("DD-7KQ4", "[code]") == AGREED_MENTOR
    # The open point stays in its brackets (D3).
    assert "[If you'd rather the channel weren't included at all" in mentor


def test_must_refuse_21_the_producers_placeholders_stay(out):
    _, _, _, said = run(team(out))
    notice, mentor = printed(said, "Channel notice"), printed(said, "Mentor message")
    assert notice.count("[close date]") == 2
    assert "[mentor]" in mentor and "#[channel]" in mentor and "[close date]" in mentor
    text = "\n".join(said)
    assert "replace each [close date] with the date 7 days after the day the notice" in text
    assert "Also replace [mentor] and #[channel]" in text


def test_it_prints_the_teams_opt_out_link_and_project_code(out):
    _, _, _, said = run(team(out))
    text = "\n".join(said)
    assert f"Opt-out link for DD-7KQ4: {LINK}" in text
    assert "  2. The project code: DD-7KQ4" in said
    assert text.count(LINK) == 3  # the line, the notice and the mentor message


def test_the_output_has_a_block_for_the_team_then_one_for_the_producer(out):
    # #156: the team's steps and what to send are apart, and nothing tells the team to run
    # the producer's commands.
    _, _, _, said = run(team(out))
    path = next(line for line in said if line.startswith("Team file: ")).removeprefix("Team file: ")
    team_block = said.index("FOR YOU (the project team)")
    send = said.index("SEND TO THE PRODUCER, privately (never in a repo, Issue, PR or channel)")
    assert team_block < send
    mine = "\n".join(said[team_block:send])
    assert "optouts DD-7KQ4 --bucket example-bucket" in mine
    assert "Then tell the producer they can run the export" in mine
    listed = said[send + 1 : send + 7]
    assert listed == [
        f"  1. The team file: {path}",
        "  2. The project code: DD-7KQ4",
        "  3. The channel notice, below.",
        "  4. The mentor message, below.",
        "  5. The producer guide.",
        "  The guide has every step they take.",
    ]


def test_the_output_has_no_producer_commands(out):
    _, _, _, said = run(team(out))
    text = "\n".join(said)
    assert "diffdata setup" not in text
    assert "Downloads/" not in text
    assert "diffdata export" not in text
    commands = [line.strip() for line in said if line.strip().startswith("uv run")]
    assert commands == ["uv run diffdata admin optouts DD-7KQ4 --bucket example-bucket"]


def test_day_7_is_optouts_then_tell_the_producer_with_no_ids_sent(out):
    _, _, _, said = run(team(out))
    text = "\n".join(said)
    command = text.index("optouts DD-7KQ4 --bucket example-bucket")
    tell = text.index("Then tell the producer they can run the export")
    assert command < tell
    assert "Don't send them the IDs" in text
    assert "Record each per-use answer (ID, uses, date) in the private log" in text  # item 3
    assert "enter only the people who opted out of everything" in text


def test_without_a_form_the_texts_keep_the_link_placeholder(out):
    _, _, _, said = run(team(out, optout_form=None))
    text = "\n".join(said)
    assert "No --optout-form given" in text
    assert "[opt-out link]" in printed(said, "Channel notice")
    assert "[opt-out link]" in printed(said, "Mentor message")


def test_must_refuse_19_the_new_lines_hold_nothing_from_the_key(out):
    _, _, _, said = run(team(out))
    assert MARKER not in "\n".join(said)


# --- project codes (must refuse 16 to 18) ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["dd-7kq4", "DD-7KQ0", "DD-7KQO", "DD-1ABC", "DD-ABCI", "DD-ABCL", "DD-ABCDE", "DD-ABC",
     "XX-7KQ4", "DD7KQ4", "team-a", " DD-7KQ4", "DD-7KQ4\n"],
)  # fmt: skip
def test_must_refuse_16_a_given_code_not_in_the_exact_form(out, name):
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="project code"):
        run(team(out, name=name), fake=fake)
    assert fake.calls == []


def test_a_code_in_the_form_is_taken_as_given(out):
    code, _, _, said = run(team(out, name="DD-Z9W2"))
    assert code == 0
    assert (out / "DD-Z9W2.diffdata-team.json").exists()
    assert not any(line.startswith("Made the project code") for line in said)


def chooser(*codes: str):
    """A stand-in for secrets.choice that spells out the given codes' 4 characters in turn."""
    letters = iter("".join(c.removeprefix("DD-") for c in codes))
    return lambda chars: next(letters)


def make_run(chosen: Team, choose, fake=None):
    fake = fake or FakeGcloud()
    said = []
    code = admin.add_team(
        chosen, ask_yes=lambda q: True, say=said.append, gcloud=fake, sleep=lambda s: 0,
        make_code=True, choose=choose,
    )  # fmt: skip
    return code, fake, said


def test_with_no_code_add_team_makes_one_and_uses_it_everywhere(out):
    code, fake, said = make_run(team(out, name=""), chooser("DD-H3XT"))
    assert code == 0
    assert said[0] == "Made the project code DD-H3XT."
    assert said[1].startswith("Team DD-H3XT, bucket")
    data = json.loads((out / "DD-H3XT.diffdata-team.json").read_text(encoding="utf-8"))
    assert data["folder"] == "DD-H3XT"
    assert "  2. The project code: DD-H3XT" in said
    made = ["storage", "managed-folders", "create", "gs://example-bucket/DD-H3XT/", "--quiet"]
    assert made in fake.changes()


def test_must_refuse_17_a_made_code_in_use_is_never_used(out):
    fake = FakeGcloud()
    fake.folders["gs://example-bucket/DD-AAAA/"] = []  # another team's, with no grants
    fake.accounts[f"dd-dd-bbbb@{GCP}.iam.gserviceaccount.com"] = "someone's"
    fake.folders["gs://example-bucket/DD-CCCC/approved/"] = []  # a folder inside it only
    code, fake, said = make_run(
        team(out, name=""), chooser("DD-AAAA", "DD-BBBB", "DD-CCCC", "DD-DDDD"),
        fake=fake,
    )  # fmt: skip
    assert code == 0
    assert said[0] == "Made the project code DD-DDDD."
    for used in ("DD-AAAA", "DD-BBBB", "DD-CCCC"):
        assert not any(used in " ".join(c) for c in fake.changes())


def test_must_refuse_17_it_stops_after_5_codes_in_use(out):
    fake = FakeGcloud()
    codes = ["DD-AAAA", "DD-BBBB", "DD-CCCC", "DD-DDDD", "DD-EEEE"]
    for c in codes:
        fake.folders[f"gs://example-bucket/{c}/"] = []
    with pytest.raises(AdminError, match="5 new project codes"):
        make_run(team(out, name=""), chooser(*codes), fake=fake)
    assert fake.changes() == []
    assert not out.exists()


def test_must_refuse_17_a_failed_look_stops_rather_than_guessing(out):
    fake = FakeGcloud()
    fake.fail["list"] = "ERROR: 403 Permission denied"
    with pytest.raises(AdminError, match="Couldn't check the upload account"):
        make_run(team(out, name=""), chooser("DD-AAAA"), fake=fake)
    assert fake.changes() == []


def test_with_no_code_the_other_values_are_checked_before_gcloud(out):
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="--producer"):
        make_run(team(out, name="", producers=()), chooser("DD-AAAA"), fake=fake)
    assert fake.calls == []


def test_must_refuse_18_a_normal_run_draws_from_secrets(out, monkeypatch):
    drawn = []

    def recording(chars):
        drawn.append(chars)
        return "Q"

    monkeypatch.setattr(admin.secrets, "choice", recording)
    code = admin.add_team(
        team(out, name=""), ask_yes=lambda q: True, say=lambda line: None, gcloud=FakeGcloud(),
        sleep=lambda s: 0, make_code=True,
    )  # fmt: skip
    assert code == 0
    assert drawn == [admin.CODE_CHARS] * 4
    assert (out / "DD-QQQQ.diffdata-team.json").exists()


def test_must_refuse_18_codes_are_in_the_form():
    for _ in range(200):
        assert admin.CODE.fullmatch(admin.new_code())
    assert not set("0O1IL") & set(admin.CODE_CHARS)


def test_must_refuse_17_files_already_under_a_made_code_rule_it_out(out):
    fake = FakeGcloud()
    fake.objects.append("gs://example-bucket/DD-AAAA/old.md")  # no managed folder, just a file
    code, fake, said = make_run(team(out, name=""), chooser("DD-AAAA", "DD-BBBB"), fake=fake)
    assert code == 0
    assert said[0] == "Made the project code DD-BBBB."
    assert not any("DD-AAAA" in " ".join(c) for c in fake.changes())


def test_must_refuse_17_a_denied_answer_never_passes_for_not_found(out):
    fake = FakeGcloud()
    real = fake.folders_command

    def denied(verb, url, flags):
        if verb == "describe":
            return fake.answer(1, err="ERROR: 403 caller lacks permission on example-404")
        return real(verb, url, flags)

    fake.folders_command = denied
    with pytest.raises(AdminError, match="Couldn't check"):
        make_run(team(out, name=""), chooser("DD-AAAA"), fake=fake)
    assert fake.changes() == []


def test_must_refuse_17_a_made_code_that_comes_into_use_meanwhile_stops(out, monkeypatch):
    fake = FakeGcloud()
    real_taken = admin.AddTeam.taken

    def then_someone_makes_it(self):
        free = real_taken(self)
        fake.folders[self.team.folder()] = []  # another run makes the folder, with no grants
        return free

    monkeypatch.setattr(admin.AddTeam, "taken", then_someone_makes_it)
    with pytest.raises(AdminError, match="came into use"):
        make_run(team(out, name=""), chooser("DD-AAAA"), fake=fake)
    assert fake.changes() == []


def test_a_local_team_file_under_a_made_code_rules_it_out(out):
    out.mkdir()
    (out / "DD-AAAA.diffdata-team.json").write_text("an older one", encoding="utf-8")
    code, _, said = make_run(team(out, name=""), chooser("DD-AAAA", "DD-BBBB"))
    assert code == 0
    assert said[0] == "Made the project code DD-BBBB."
    assert (out / "DD-AAAA.diffdata-team.json").read_text(encoding="utf-8") == "an older one"


def test_must_refuse_19_nothing_from_the_key_on_stdout_or_stderr(out, capsys):
    code = admin.add_team(
        team(out), ask_yes=lambda q: True, gcloud=FakeGcloud(), sleep=lambda s: 0
    )  # say is print
    assert code == 0
    printed_out = capsys.readouterr()
    assert MARKER not in printed_out.out + printed_out.err
    assert "Channel notice" in printed_out.out


def cli_args(*code: str, out: Path) -> list[str]:
    return [
        "admin", "add-team", *code, "--bucket", "example-bucket", "--project", "example-project",
        "--gcp-project", GCP, "--producer", "producer-one@example.com", "--out", str(out),
    ]  # fmt: skip


def test_must_refuse_16_an_empty_code_on_the_command_line_is_refused(monkeypatch, capsys, out):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setattr(admin, "run_gcloud", lambda args: pytest.fail("gcloud ran"))
    assert cli.main(cli_args("", out=out)) == 1
    assert "project code" in capsys.readouterr().err


def test_a_left_out_code_on_the_command_line_makes_one(monkeypatch):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    seen = {}

    def fake_add_team(team, ask_yes, make_code=False, **kw):
        seen.update(name=team.name, make_code=make_code)
        return 0

    monkeypatch.setattr(admin, "add_team", fake_add_team)
    assert cli.main(cli_args(out=Path("/nonexistent"))) == 0
    assert seen == {"name": "", "make_code": True}
    assert cli.main(cli_args("DD-7KQ4", out=Path("/nonexistent"))) == 0
    assert seen == {"name": "DD-7KQ4", "make_code": False}


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
    assert f"Team file: {out / 'DD-7KQ4.diffdata-team.json'}" in said


def test_must_refuse_4_a_folder_where_the_team_file_goes_stops_before_any_change(out):
    fake = FakeGcloud()
    out.mkdir()
    (out / "DD-7KQ4.diffdata-team.json").mkdir()  # in the way before the run starts
    with pytest.raises(AdminError, match="never replaces one") as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert fake.changes() == []  # caught before any change


def test_must_refuse_3_a_team_file_that_appears_while_gcloud_runs_isnt_replaced(out):
    fake = FakeGcloud()
    real = fake.accounts_command

    def appear(words, flags):
        if words[:2] == ["keys", "create"]:
            out.mkdir(exist_ok=True)
            (out / "DD-7KQ4.diffdata-team.json").write_text("someone's file", encoding="utf-8")
        return real(words, flags)

    fake.accounts_command = appear
    with pytest.raises(AdminError) as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert "deleted the new key again" in str(stopped.value)
    assert fake.keys[EMAIL] == 0  # the key no team file holds is gone, so a rerun can finish
    assert (out / "DD-7KQ4.diffdata-team.json").read_text(encoding="utf-8") == "someone's file"
    assert [p.name for p in out.iterdir()] == ["DD-7KQ4.diffdata-team.json"]


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


@POSIX
def test_must_refuse_4_not_through_a_link_into_a_repository(tmp_path):
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    link = tmp_path / "teams-link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(AdminError, match="git repository or a synced folder"):
        run(team(link))


def test_must_refuse_4_never_replaces_a_team_file(out):
    out.mkdir()
    (out / "DD-7KQ4.diffdata-team.json").write_text("an older one", encoding="utf-8")
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="never replaces one"):
        run(team(out), fake=fake)
    assert fake.calls == []
    assert (out / "DD-7KQ4.diffdata-team.json").read_text(encoding="utf-8") == "an older one"


@POSIX
def test_must_refuse_4_the_team_file_is_readable_only_by_you(out):
    run(team(out))
    mode = stat.S_IMODE((out / "DD-7KQ4.diffdata-team.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


# --- must refuse 5: names push would refuse -------------------------------------------------


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"name": "team/a"}, "team name"),
        ({"name": "team a"}, "team name"),
        ({"name": "../DD-7KQ4"}, "team name"),
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
        ["admin", "add-team", "DD-7KQ4", "--bucket", "example-bucket", "--project",
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
    fake.accounts[EMAIL] = "diffdata upload: other-bucket/DD-7KQ4"
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


# --- review 1 -----------------------------------------------------------------------------


def test_must_refuse_2_a_conditional_grant_stops(out):
    fake = FakeGcloud()
    fake.folders[ROOT] = [
        {
            "role": VIEWER,
            "members": ["user:producer-one@example.com"],
            "condition": {"title": "made up", "expression": "true"},
        }
    ]
    with pytest.raises(AdminError, match="another team's folder"):
        run(team(out), fake=fake)
    assert fake.changes() == []


def test_ctrl_c_after_the_key_deletes_it_and_lists_the_changes(out, monkeypatch):
    def stop(self, key):
        raise KeyboardInterrupt

    monkeypatch.setattr(admin.AddTeam, "write_team_file", stop)
    fake = FakeGcloud()
    with pytest.raises(AdminError) as stopped:
        run(team(out), fake=fake)
    text = str(stopped.value)
    assert text.startswith("Stopped by Ctrl-C")
    assert f"made {ROOT}_config/" in text and "deleted the new key again" in text
    assert fake.keys[EMAIL] == 0 and MARKER not in text


def test_ctrl_c_before_any_change_says_nothing_changed(out, monkeypatch):
    def stop(self, account_there, present):
        raise KeyboardInterrupt

    monkeypatch.setattr(admin.AddTeam, "make", stop)
    with pytest.raises(AdminError, match="Nothing was changed"):
        run(team(out))


# --- Codex review 2 -----------------------------------------------------------------------


def test_a_staging_file_that_cant_be_made_deletes_the_new_key(out, monkeypatch):
    real_open = os.open

    def refuse(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("made up")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(admin.os, "open", refuse)
    fake = FakeGcloud()
    with pytest.raises(AdminError, match="deleted the new key again") as stopped:
        run(team(out), fake=fake)
    assert fake.keys[EMAIL] == 0 and MARKER not in str(stopped.value)


def test_ctrl_c_after_the_team_file_is_written_keeps_its_key(out, monkeypatch):
    real_link = os.link

    def link_then_stop(src, dst):
        real_link(src, dst)
        if str(dst).endswith(".diffdata-team.json"):
            raise KeyboardInterrupt  # before the run notes that the file is in place

    monkeypatch.setattr(admin.os, "link", link_then_stop)
    fake = FakeGcloud()
    with pytest.raises(admin.Cancelled, match="after the team file was written"):
        run(team(out), fake=fake)
    assert fake.keys[EMAIL] == 1
    assert [p.name for p in out.iterdir()] == ["DD-7KQ4.diffdata-team.json"]


def test_ctrl_c_while_gcloud_makes_the_key_says_to_check(out):
    fake = FakeGcloud()
    real = fake.accounts_command

    def stop(words, flags):
        if words[:2] == ["keys", "create"]:
            real(words, flags)
            raise KeyboardInterrupt
        return real(words, flags)

    fake.accounts_command = stop
    with pytest.raises(admin.Cancelled, match="check the account's keys") as stopped:
        run(team(out), fake=fake)
    assert MARKER not in str(stopped.value)
    assert not any(p.name.startswith(".diffdata-key-") for p in out.iterdir())


def test_the_command_returns_130_on_ctrl_c(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)

    def cancelled(team, ask_yes, make_code=False):
        raise admin.Cancelled("Stopped by Ctrl-C. Nothing was changed.")

    monkeypatch.setattr(admin, "add_team", cancelled)
    code = cli.main(
        ["admin", "add-team", "DD-7KQ4", "--bucket", "example-bucket", "--project",
         "example-project", "--gcp-project", GCP, "--producer", "producer-one@example.com",
         "--out", str(tmp_path / "teams")]
    )  # fmt: skip
    assert code == 130
    assert "Stopped by Ctrl-C" in capsys.readouterr().err
