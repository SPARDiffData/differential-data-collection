"""`diffdata admin add-team` and `optouts` (STO-1, CON-4). Plan: Issue #132, decisions 1, 2, 3,
5 and 6, PRs 2 and 3.

The project team runs it once per pilot team, in their own terminal, signed in to `gcloud`
as the Cloud project's owner. It does what was done by hand for the first test folder:

  1. makes a service account for the team (its upload account), with no project role;
  2. makes the team's managed folder in the bucket, and three folders inside it:
     approved/ and not-approved/ (where `diffdata push` writes) and _config/ (where
     `diffdata admin optouts` will write the opted-out list, #132 PR 3);
  3. grants the upload account Storage Object Creator on approved/ and not-approved/, and
     Storage Object Viewer on _config/; and grants each producer's Google account Storage
     Object Viewer on the team's folder;
  4. makes the account's key, and writes one team file, <team>.diffdata-team.json, that
     `diffdata setup` reads (#131).

Then it prints the producer's steps in order, day 7's steps, the team's pre-filled opt-out link,
and the agreed channel notice and mentor message (decision 5; #150 items 1, 3, 4 and 6).

The team name is its project code (#150, item 2): DD- and 4 capitals or digits with no 0, O, 1,
I or L. Left out, add-team makes one.

Why Creator on approved/ and not-approved/, not on the whole folder: a grant on a managed
folder also covers every folder inside it. Creator on the whole folder would let the team's
key add _config/opted-out.json before the project team does, and so choose the opt-out list.
Push writes only under those two folders (store/push.py, Settings.prefix).

All changes go through the `gcloud` command, so there's no new dependency. Nothing changes
before the project team has seen the plan and typed y.

What it refuses (#132's must-refuse list for add-team, approved 2026-11-25):
  1  to grant the upload account anything but Object Creator on its own folder (here, its
     approved/ and not-approved/ parts) and Object Viewer on <folder>/_config/. It never
     changes the project's or the bucket's policy, and grants producers Object Viewer only
  2  to touch another team's folder: if the team's folder, or one of its four folders,
     already has a member this run wouldn't add, or the team's folder holds any other managed
     folder (it would get the new grants by inheritance), it stops before changing anything.
     It looks again after the yes. An upload account of that name made for another team or
     bucket, or one that already has a key, stops it too
  3  to print the key: the run reports counts and the team file's path only. gcloud runs with
     its logs off, and its messages from the key step aren't shown
  4  to write the team file inside a git repository or a synced folder, or readable by anyone
     but this user; it never replaces a team file that's there. It runs on macOS and Linux
     only, where a file mode keeps the file private. If the team file can't be written, it
     deletes the key it just made
  5  a folder (the team name), project or bucket that `push` would refuse

Project codes and the texts (#150, approved 2026-11-26):
  16 a given code not exactly in the form: refused, never changed to fit (a lowercase code too)
  17 to make a code that's in use: one whose upload account, folder, any folder inside it, any
     file under it in the bucket, or local team file is there already is never used; it tries
     another, up to 5 times, then stops. A "not found" that also says access was denied stops
     the run. A made code must still be unused when add-team looks again, or it stops. A given
     code keeps items 1 to 5 (a re-run finishes a partial run; another team's folder stops it)
  18 to make a code from anything but a secure random source (`secrets`)
  19 to print anything from the key: the texts and steps hold the code, links and paths only
  20 to print the notice or the mentor message in any wording but the agreed text (with
     #150's D15 line)
  21 to fill in [close date], [mentor] or #[channel]: they're the producer's

`diffdata admin optouts <team> --bucket <bucket>` runs on day 7, after the project team has
looked at the form's answers and found each person's Slack user ID. It writes the team's
opted-out list to <team>/_config/opted-out.json, which the producer's export fetches
(discover/team_optouts.py). The IDs are pasted at a prompt, never given on the command line.
  9  it never writes a list that hasn't been looked at: it shows the team, the count and the
     IDs to add, and asks for a yes. A list already there is only added to, never shortened,
     and the write is refused if the list changed after it was read
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from diffdata.common import setup as producer_setup
from diffdata.common.config import USER_ID
from diffdata.common.paths import find_git_root, synced_by
from diffdata.discover import team_optouts
from diffdata.store import push

CREATOR = "roles/storage.objectCreator"
VIEWER = "roles/storage.objectViewer"
PARTS = (push.APPROVED, push.NOT_APPROVED)
CONFIG = "_config"
DEFAULT_OUT = "~/diffdata-teams"
TEAM_SUFFIX = ".diffdata-team.json"
# A service account ID: 6 to 30 lowercase letters, digits and -, starting with a letter.
ACCOUNT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
GCP_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOT_FOUND = re.compile(r"\b404\b|not[ _]?found|does not exist", re.IGNORECASE)
DENIED = re.compile(r"\b40[13]\b|denied|forbidden|permission|unauthori[sz]ed", re.IGNORECASE)
NO_FILES = re.compile(r"matched no objects", re.IGNORECASE)
NOTHING_CHANGED = "Nothing was changed."
WINDOWS = os.name == "nt"
# gcloud's own logs could hold the key-making answer, which holds the key: keep them off.
QUIET_GCLOUD = {
    "CLOUDSDK_CORE_LOG_HTTP": "false",
    "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true",
    "CLOUDSDK_CORE_VERBOSITY": "error",
}
# A project code (#150, item 2): DD- and 4 letters or digits that can't be confused (no 0, O,
# 1, I or L). It's the team's folder name, the team file's `folder`, and the notice's code.
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE = re.compile(rf"DD-[{CODE_CHARS}]{{4}}")
CODE_TRIES = 5
CODE_HELP = (
    "A project code is DD- and 4 capital letters or digits, with no 0, O, 1, I or L, like DD-7KQ4"
)
# The agreed texts, from the sign-up procedure doc ("Message texts"), word for word, with one
# line changed as approved on #150 (D15). add-team fills in [code] and [opt-out link] only:
# [close date], [mentor] and [channel] are the producer's to fill in (D1, D3).
CODE_FIELD = "[code]"
LINK_FIELD = "[opt-out link]"
NOTICE = """\
**Heads up: I've signed this channel up for the SPAR project *Differential Data for Automated AI Safety Research*. Project code: [code]**

The project collects the messages and docs where research decisions actually get made, the part final papers leave out, to help AI safety research. About the project: https://docs.google.com/document/d/1ggbdAYohoFG06BrUmxuWczUXqiHAqD3CBWN2z3ShW54/edit

- **On [close date] I'll run their tool on this channel.** It leaves out anyone who opted out, pseudonymises names (replacing them with codes like [Person 3]) and removes contact details and secrets, and I review everything before it's uploaded.
- **The scrubbed data goes to the project's storage** and is shared only with approved research groups, each for one use: control evaluations and conceptual reasoning at Redwood Research, and SPAR's study of how research programs run. Some people think conceptual reasoning work speeds up AI capabilities, so you can leave your data out of that use alone.
- **By [close date], use the opt-out form ([opt-out link]) with project code [code]** to opt out of everything, or of particular uses. You don't need to give your name or a reason. After the 7 days, the project team gives my copy of the tool the list of who to leave out.
- **You can change your choices or object any time after that,** with the same form, and they'll act on it within 14 days.

Please keep sensitive or private personal details out of this channel. Questions: ask me, or spardiffdata＠gmail.com. Privacy notice: https://docs.google.com/document/d/10lLqAHVoNYXux-fWmzYfOuZj-ywjsMOZwfbTZs-Rvkc/edit"""  # noqa: E501
MENTOR = """\
Hi [mentor], a heads-up: I've signed our project channel #[channel] up for the SPAR project *Differential Data for Automated AI Safety Research*. It collects research discussions, scrubbed and reviewed by me first, for AI safety research. Nothing is collected before [close date]. If you'd like your messages and edits left out, or kept out of particular uses such as conceptual reasoning, use the opt-out form ([opt-out link]) with project code [code], or just tell me. [If you'd rather the channel weren't included at all, tell me and I'll withdraw it.] More: About the project https://docs.google.com/document/d/1ggbdAYohoFG06BrUmxuWczUXqiHAqD3CBWN2z3ShW54/edit, Privacy notice https://docs.google.com/document/d/10lLqAHVoNYXux-fWmzYfOuZj-ywjsMOZwfbTZs-Rvkc/edit"""  # noqa: E501


class AdminError(Exception):
    """add-team can't go on. The message says what was done so far, and quotes no key."""


class Cancelled(AdminError):
    """Stopped by Ctrl-C while making changes. The message says what was done so far."""


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run_gcloud(args: list[str]) -> subprocess.CompletedProcess:
    """Run one gcloud command, its output captured, never shown as is."""
    found = shutil.which("gcloud")
    if found is None:
        raise AdminError(f"gcloud isn't installed, or isn't on PATH. {NOTHING_CHANGED}")
    return subprocess.run(
        [found, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        env={**os.environ, **QUIET_GCLOUD},
    )


@dataclass(frozen=True)
class Team:
    """What add-team was asked to set up, checked."""

    name: str
    bucket: str
    project: str
    gcp_project: str
    producers: tuple[str, ...]
    out: Path
    optout_form: str | None = None
    google_client: dict | None = None

    @property
    def account(self) -> str:
        return f"dd-{self.name.lower().replace('_', '-')}"

    @property
    def account_email(self) -> str:
        return f"{self.account}@{self.gcp_project}.iam.gserviceaccount.com"

    @property
    def display_name(self) -> str:
        # Holds the bucket too, so a second run for another bucket can't reuse the account.
        return f"diffdata upload: {self.bucket}/{self.name}"

    @property
    def team_file(self) -> Path:
        return self.out / f"{self.name}{TEAM_SUFFIX}"

    def folder(self, part: str = "") -> str:
        return f"gs://{self.bucket}/{self.name}/" + (f"{part}/" if part else "")

    def grants(self) -> dict[str, set[tuple[str, str]]]:
        """Every grant this run makes: folder -> {(role, member)}. Nothing else is granted."""
        upload = f"serviceAccount:{self.account_email}"
        found = {self.folder(): {(VIEWER, f"user:{p}") for p in self.producers}}
        for part in PARTS:
            found[self.folder(part)] = {(CREATOR, upload)}
        found[self.folder(CONFIG)] = {(VIEWER, upload)}
        return found


def check_team(team: Team) -> Team:
    """Item 5, and the rest of what add-team needs, checked before anything is asked."""
    if WINDOWS:
        # A file mode doesn't keep a team file private on Windows (item 4).
        raise AdminError(f"Run add-team on macOS or Linux. {NOTHING_CHANGED}")
    if not CODE.fullmatch(team.name):
        # Refused, never changed to fit: a code typed in lowercase is shown, not guessed (D4).
        raise AdminError(
            f"The team name must be its project code. {CODE_HELP}. Leave it out and add-team "
            f"makes one. {NOTHING_CHANGED}"
        )
    try:
        producer_setup.check_team(
            {"bucket": team.bucket, "folder": team.name, "project": team.project}
        )
    except producer_setup.SetupError as e:
        raise AdminError(
            f"{str(e).replace('folder ', 'The team name ', 1)} {NOTHING_CHANGED}"
        ) from None
    if not ACCOUNT_ID.fullmatch(team.account):
        raise AdminError(
            f"The team name makes the upload account name {team.account}, which Google refuses: "
            f"use 3 to 27 letters, digits, - and _, ending in a letter or digit. {NOTHING_CHANGED}"
        )
    if len(team.display_name) > 100:
        raise AdminError(f"The bucket and team names are too long together. {NOTHING_CHANGED}")
    if not GCP_PROJECT.fullmatch(team.gcp_project):
        raise AdminError(f"--gcp-project isn't a Google Cloud project ID. {NOTHING_CHANGED}")
    if not team.producers:
        raise AdminError(f"Give each producer's Google account with --producer. {NOTHING_CHANGED}")
    for producer in team.producers:
        if not EMAIL.fullmatch(producer):
            raise AdminError(f"--producer {producer!r} isn't one email address. {NOTHING_CHANGED}")
    if team.optout_form is not None and not (
        team.optout_form.startswith("https://") and team.optout_form.endswith("=")
    ):
        raise AdminError(
            "--optout-form must be the form's pre-filled link, up to and including the team "
            f"field's '=' (https://…&entry.123=). {NOTHING_CHANGED}"
        )
    if team.google_client is not None:
        installed = team.google_client.get("installed")
        if not isinstance(installed, dict) or not isinstance(installed.get("client_id"), str):
            raise AdminError(f"--google-client isn't a Desktop app client file. {NOTHING_CHANGED}")
    check_out(team)
    return team


def check_out(team: Team) -> None:
    """Item 4: the team file's folder is outside any repository or synced folder, and there's
    no team file there already."""
    out = team.out
    for candidate in (out.absolute(), out.resolve()):
        if find_git_root(candidate) is not None or synced_by(candidate) is not None:
            raise AdminError(
                f"{out} is inside a git repository or a synced folder, where the team file "
                f"could be shared by mistake. Choose another --out. {NOTHING_CHANGED}"
            )
    if out.exists() and not out.is_dir():
        raise AdminError(f"{out} isn't a folder. {NOTHING_CHANGED}")
    if os.path.lexists(team.team_file):
        raise AdminError(
            f"There's a team file at {team.team_file} already. Move it away first: add-team "
            f"never replaces one. {NOTHING_CHANGED}"
        )


def read_client(path: Path) -> dict:
    """The Google client file the producer's sign-in uses. Never shown."""
    raw = push.read_plain(path.expanduser().parent, path.name)
    try:
        data = json.loads(raw.decode("utf-8")) if raw and len(raw) < 64_000 else None
    except (UnicodeDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise AdminError(f"--google-client isn't a plain JSON file. {NOTHING_CHANGED}")
    return data


@dataclass
class AddTeam:
    """One run. `gcloud` and `sleep` are replaced in tests; `done` lists what changed, so a
    run that stops partway says what to undo."""

    team: Team
    gcloud: Runner = run_gcloud
    sleep: Callable[[float], None] = time.sleep
    done: list[str] = field(default_factory=list)
    key: bytes | None = None  # the key this run made, once read
    key_asked: bool = False  # gcloud was asked for a key, so one may exist
    published: bool = False  # the team file holding the key is in place

    def call(self, *args: str, what: str) -> subprocess.CompletedProcess:
        result = self.gcloud([*args, "--quiet"])
        if result.returncode != 0:
            raise AdminError(self.stopped(f"{what} failed: {last_line(result.stderr)}"))
        return result

    def stopped(self, why: str) -> str:
        if not self.done:
            return f"{why}. {NOTHING_CHANGED}"
        return f"{why}. Changed so far, to undo by hand if you won't run again: " + "; ".join(
            self.done
        )

    def exists(self, *args: str, what: str) -> dict | None:
        """A describe or get command's JSON, or None if the thing isn't there."""
        result = self.gcloud([*args, "--format=json", "--quiet"])
        if result.returncode != 0:
            if NOT_FOUND.search(result.stderr or ""):
                return None
            raise AdminError(self.stopped(f"Couldn't check {what}: {last_line(result.stderr)}"))
        try:
            return json.loads(result.stdout or "{}")
        except ValueError:
            raise AdminError(self.stopped(f"gcloud's answer about {what} isn't JSON")) from None

    def listing(self, *args: str, what: str) -> list:
        """A list command's JSON. "Not found" is an empty list; any other failure stops."""
        found = self.exists(*args, what=what)
        if found is None:
            return []
        if not isinstance(found, list):
            raise AdminError(self.stopped(f"gcloud's answer about {what} isn't a list"))
        return found

    # --- looking first: nothing below changes anything ---------------------------------------

    def strict(self, args: list[str], what: str) -> object | None:
        """A command's JSON, or None only for a plain "not found". For a new code, a refusal
        must never pass for "nothing there", so an answer that also says the access was
        denied stops the run (must-refuse 17)."""
        result = self.gcloud([*args, "--format=json", "--quiet"])
        stderr = result.stderr or ""
        if result.returncode != 0:
            if NOT_FOUND.search(stderr) and not DENIED.search(stderr):
                return None
            raise AdminError(self.stopped(f"Couldn't check {what}: {last_line(stderr)}"))
        try:
            return json.loads(result.stdout or "null")
        except ValueError:
            raise AdminError(self.stopped(f"gcloud's answer about {what} isn't JSON")) from None

    def taken(self) -> bool:
        """Whether a new code is in use already: its upload account, its folder, any folder
        inside it, or any file under its name in the bucket (a new managed folder's access
        would cover files already there). Must-refuse 17. Any other failure stops the run."""
        team = self.team
        root = team.folder()
        accounts = self.strict(
            ["iam", "service-accounts", "list", f"--project={team.gcp_project}",
             f"--filter=email={team.account_email}"],
            "the upload account",
        )  # fmt: skip
        if not isinstance(accounts, list):
            raise AdminError(self.stopped("gcloud's answer about the upload account isn't a list"))
        if any(isinstance(a, dict) and a.get("email") == team.account_email for a in accounts):
            return True
        if self.strict(["storage", "managed-folders", "describe", root], root) is not None:
            return True
        if self.strict(["storage", "managed-folders", "list", root], f"the folders in {root}"):
            return True
        return self.has_files(root)

    def has_files(self, root: str) -> bool:
        """Whether any file sits under `root`. gcloud says "matched no objects" when none do."""
        result = self.gcloud(["storage", "ls", f"{root}**", "--quiet"])
        if result.returncode == 0:
            return bool((result.stdout or "").strip())
        stderr = result.stderr or ""
        if NO_FILES.search(stderr) and not DENIED.search(stderr):
            return False
        raise AdminError(self.stopped(f"Couldn't check the files in {root}: {last_line(stderr)}"))

    def look(self) -> tuple[bool, list[str]]:
        """Whether the account is there already, and which folders are. Stops on anything
        this run didn't make (item 2)."""
        team = self.team
        # Not `describe`: for an account that isn't there, Google can answer "permission denied".
        accounts = self.listing(
            "iam", "service-accounts", "list", f"--project={team.gcp_project}",
            f"--filter=email={team.account_email}", what="the upload account",
        )  # fmt: skip
        email = team.account_email
        matching = [a for a in accounts if isinstance(a, dict) and a.get("email") == email]
        account = matching[0] if matching else None
        if account is not None:
            if account.get("displayName") != team.display_name:
                raise AdminError(
                    f"There's an account named {team.account} already, not made by add-team "
                    f"for this team. Choose another team name. {NOTHING_CHANGED}"
                )
            keys = self.exists(
                "iam", "service-accounts", "keys", "list", f"--iam-account={team.account_email}",
                f"--project={team.gcp_project}", "--managed-by=user", what="the account's keys",
            )  # fmt: skip
            if keys:
                raise AdminError(
                    f"The upload account {team.account} has a key already, so a team file for it "
                    "was made before. Delete that key in the Cloud console first, then run "
                    f"again. {NOTHING_CHANGED}"
                )
        # Any managed folder under the team's folder that this run doesn't make would get the
        # new grants by inheritance, so one there already stops the run.
        expected = set(team.grants())
        root = team.folder()
        inside = self.listing(
            "storage", "managed-folders", "list", root, what=f"the folders in {root}"
        )  # fmt: skip
        for entry in inside:
            url = folder_url(entry)
            if url is None or (url.startswith(team.folder()) and url not in expected):
                raise AdminError(
                    f"{team.folder()} already holds a folder this run wouldn't make "
                    f"({url or 'one gcloud did not name'}). It may be another team's folder: "
                    f"stopping. {NOTHING_CHANGED}"
                )
        present = []
        for folder, grants in team.grants().items():
            if self.exists("storage", "managed-folders", "describe", folder, what=folder) is None:
                continue
            present.append(folder)
            policy = self.exists(
                "storage", "managed-folders", "get-iam-policy", folder, what=f"{folder}'s access"
            )
            for binding in (policy or {}).get("bindings", []):
                for member in binding.get("members", []):
                    # A grant with a condition isn't one this run makes, even for our member.
                    if "condition" in binding or (binding.get("role"), member) not in grants:
                        raise AdminError(
                            f"{folder} exists already, and someone this run wouldn't add has "
                            f"access to it. It may be another team's folder: stopping. "
                            f"{NOTHING_CHANGED}"
                        )
        return account is not None, present

    def plan(self, account_there: bool, present: list[str]) -> list[str]:
        team = self.team
        lines = [f"Team {team.name}, bucket {team.bucket}, Cloud project {team.gcp_project}:"]
        if not account_there:
            lines.append(f"  make the upload account {team.account_email}, with no project role")
        for folder in team.grants():
            if folder not in present:
                lines.append(f"  make the managed folder {folder}")
        for folder, grants in team.grants().items():
            for role, member in sorted(grants):
                lines.append(f"  grant {role} on {folder} to {member}")
        lines.append("  make the account's key, and put it only in the team file")
        lines.append(f"  write {team.team_file}, readable only by you")
        return lines

    # --- changing ---------------------------------------------------------------------------

    def prepare_out(self) -> None:
        """The team file's folder, made before any cloud change, so a local failure costs
        nothing in the cloud."""
        try:
            self.team.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as e:
            raise AdminError(
                f"Couldn't make {self.team.out} ({type(e).__name__}). {NOTHING_CHANGED}"
            ) from None

    def make(self, account_there: bool, present: list[str]) -> bytes:
        """Make the account, the folders and the grants, then the key. Returns the key."""
        team = self.team
        if not account_there:
            self.call(
                "iam", "service-accounts", "create", team.account,
                f"--project={team.gcp_project}", f"--display-name={team.display_name}",
                what="Making the upload account",
            )  # fmt: skip
            self.done.append(f"made the account {team.account_email}")
        for folder in team.grants():  # the team's folder first, then the ones inside it
            if folder not in present:
                self.call("storage", "managed-folders", "create", folder, what=f"Making {folder}")
                self.done.append(f"made {folder}")
        for folder, grants in team.grants().items():
            for role, member in sorted(grants):
                self.grant(folder, role, member)
        return self.make_key()

    def grant(self, folder: str, role: str, member: str) -> None:
        args = [
            "storage", "managed-folders", "add-iam-policy-binding", folder,
            f"--member={member}", f"--role={role}",
        ]  # fmt: skip
        # A new account can take a few seconds to be known to Cloud Storage.
        for wait in (2, 4, 8, 16, None):
            result = self.gcloud([*args, "--quiet"])
            if result.returncode == 0:
                self.done.append(f"granted {role} on {folder} to {member}")
                return
            if wait is None or not NOT_FOUND.search(result.stderr or ""):
                raise AdminError(
                    self.stopped(f"Granting access on {folder} failed: {last_line(result.stderr)}")
                )
            self.sleep(wait)

    def make_key(self) -> bytes:
        """The account's new key. gcloud writes it to a file in a private temporary folder
        inside the checked --out folder, deleted straight after it's read. Never shown, and
        gcloud's own messages from this step aren't shown either."""
        team = self.team
        with tempfile.TemporaryDirectory(prefix=".diffdata-key-", dir=team.out) as temp:
            os.chmod(temp, 0o700)
            path = Path(temp) / "key.json"
            self.key_asked = True
            result = self.gcloud(
                [
                    "iam", "service-accounts", "keys", "create", str(path),
                    f"--iam-account={team.account_email}", f"--project={team.gcp_project}",
                    "--quiet",
                ]
            )  # fmt: skip
            if result.returncode != 0:
                path.unlink(missing_ok=True)
                raise AdminError(
                    self.stopped(
                        "Making the account's key failed (gcloud's message isn't shown, as it "
                        "could hold the key). An organisation policy may block keys: stop and "
                        "tell the team"
                    )
                )
            self.done.append(
                f"made a key for {team.account_email} (delete it in the Cloud console if no "
                "team file was written)"
            )
            try:
                raw = self.key = path.read_bytes()
            finally:
                path.unlink(missing_ok=True)
        try:
            push.check_key_info(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, push.PushError):
            raise AdminError(
                self.stopped("The key gcloud made isn't a service account key")
            ) from None
        return raw

    def delete_key(self, key: bytes) -> bool:
        """Delete the key this run made, when no team file holds it. True if it's gone."""
        try:
            key_id = json.loads(key.decode("utf-8")).get("private_key_id")
        except (UnicodeDecodeError, ValueError, AttributeError):
            key_id = None
        if not isinstance(key_id, str) or not key_id:
            return False
        result = self.gcloud(
            [
                "iam", "service-accounts", "keys", "delete", key_id,
                f"--iam-account={self.team.account_email}", f"--project={self.team.gcp_project}",
                "--quiet",
            ]
        )  # fmt: skip
        if result.returncode != 0:
            return False
        self.done = [d for d in self.done if not d.startswith("made a key")]
        self.done.append("deleted the new key again, as no team file holds it")
        return True

    def write_team_file(self, key: bytes) -> Path:
        """Item 4: written as a new file only this user can read, then read back with setup's
        own reader. Never replaces a file."""
        team = self.team
        data = {
            "format": producer_setup.TEAM_FORMAT,
            "version": producer_setup.TEAM_VERSION,
            "bucket": team.bucket,
            "folder": team.name,
            "project": team.project,
            "store_key": json.loads(key.decode("utf-8")),
        }
        if team.google_client is not None:
            data["google_client"] = team.google_client
        try:
            check_out(team)  # again: something may have appeared while gcloud ran
        except AdminError as e:
            raise AdminError(
                self.stopped(str(e).removesuffix(f" {NOTHING_CHANGED}").rstrip("."))
            ) from None
        temp = team.out / f".{team.name}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            producer_setup.read_team_file(temp)
            os.link(temp, team.team_file)  # fails if a file appeared there: nothing replaced
            self.published = True
        except (OSError, producer_setup.SetupError) as e:
            raise AdminError(
                self.stopped(f"Couldn't write the team file ({type(e).__name__})")
            ) from None
        finally:
            temp.unlink(missing_ok=True)
        return team.team_file


def folder_url(entry) -> str | None:
    """A managed folder's gs:// address, from one entry of `managed-folders list`."""
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("url"), str):
        return entry["url"]
    bucket, name = entry.get("bucket"), entry.get("name")
    if isinstance(bucket, str) and isinstance(name, str):
        return f"gs://{bucket}/{name.rstrip('/')}/"
    return None


def last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


def optout_link(team: Team) -> str | None:
    if team.optout_form is None:
        return None
    return team.optout_form + urllib.parse.quote(team.name, safe="")


def new_code(choose: Callable[[str], str] | None = None) -> str:
    """A new project code, from a secure random source (must-refuse 18). `choose` is for tests."""
    choose = choose or secrets.choice
    return "DD-" + "".join(choose(CODE_CHARS) for _ in range(4))


def pick_code(team: Team, gcloud: Runner, choose: Callable[[str], str] | None) -> Team:
    """The team, with a new project code nobody uses yet: a code whose upload account, folders,
    files or local team file are there already is never used (must-refuse 17). The local checks
    run before gcloud is asked anything."""
    for _ in range(CODE_TRIES):
        candidate = replace(team, name=new_code(choose))
        if os.path.lexists(candidate.team_file):
            continue
        check_team(candidate)
        if not AddTeam(candidate, gcloud=gcloud).taken():
            return candidate
    raise AdminError(
        f"Each of {CODE_TRIES} new project codes was in use already. Run again, or give a code. "
        f"{NOTHING_CHANGED}"
    )


def filled(text: str, team: Team, link: str | None) -> str:
    """An agreed text with the team's code and link put in. Nothing else is filled in."""
    return text.replace(CODE_FIELD, team.name).replace(LINK_FIELD, link or LINK_FIELD)


def next_steps(team: Team, path: Path, link: str | None) -> list[str]:
    """What add-team prints after the team file: the order of the producer's steps (#150, item
    6), day 7 (items 3 and 4), then the notice and the mentor message (item 1)."""
    lines = [
        f"Project code: {team.name}",
        "Send the team file to the team's producer privately (never in a repo, Issue, PR or "
        "channel), with the project code and the two texts below. Their steps, in this order:",
        "  1. Post the channel notice in each channel, and pin it.",
        "  2. Send the mentor message to the mentor, in Slack.",
        "  3. Run setup once, in their own terminal, with the team file where they saved it, and "
        "paste each pinned notice's link when it asks:",
        f"       uv run diffdata setup ~/Downloads/{path.name}",
        f"On day 7, when every channel's 7 days are over: look at the form's answers for "
        f"{team.name} and find each person's Slack user ID. Record each per-use answer (ID, uses, "
        "date) in the private log, outside the repo. Then run this, and enter only the people "
        "who opted out of everything:",
        f"       uv run diffdata admin optouts {team.name} --bucket {team.bucket}",
        "  Then tell the producer they can run the export. Don't send them the IDs: the export "
        "fetches the list itself.",
    ]
    if link is None:
        lines.append(
            "No --optout-form given: the team's link is the form's pre-filled link followed by "
            f"{urllib.parse.quote(team.name, safe='')}. Put it in place of {LINK_FIELD} in both "
            "texts by hand."
        )
    else:
        lines.append(f"Opt-out link for {team.name}: {link}")
    lines += [
        "",
        "Before posting: replace each [close date] with the date 7 days after the day the notice "
        "is posted.",
        "",
        "Channel notice (posted by the producer, then pinned):",
        "",
        filled(NOTICE, team, link),
        "",
        "Mentor message (from the producer, in Slack). Also replace [mentor] and #[channel]:",
        "",
        filled(MENTOR, team, link),
    ]
    return lines


def add_team(
    team: Team,
    ask_yes: Callable[[str], bool],
    say: Callable[[str], None] = print,
    gcloud: Runner = run_gcloud,
    sleep: Callable[[float], None] = time.sleep,
    make_code: bool = False,
    choose: Callable[[str], str] | None = None,
) -> int:
    """Look, show the plan, ask, then make it all and write the team file. With make_code (no
    code given), make a new project code first (#150, item 2): team.name is then ignored."""
    if make_code:
        team = pick_code(team, gcloud, choose)
        say(f"Made the project code {team.name}.")
    run = AddTeam(check_team(team), gcloud=gcloud, sleep=sleep)
    account_there, present = run.look()
    if make_code and (account_there or present):
        # Someone made it between the check and now: a new code finishes nobody's run (17).
        raise AdminError(
            f"The new project code {team.name} came into use while add-team ran. Run again. "
            f"{NOTHING_CHANGED}"
        )
    for line in run.plan(account_there, present):
        say(line)
    if not ask_yes("Make these changes?"):
        say(NOTHING_CHANGED)
        return 1
    # Look again: someone may have changed the folders while the plan was on screen.
    if run.look() != (account_there, present):
        raise AdminError(
            f"The folders or the account changed while you read the plan. {NOTHING_CHANGED}"
        )
    run.prepare_out()
    try:
        path = run.write_team_file(run.make(account_there, present))
    except AdminError as e:
        # Take back a key no team file holds, so a rerun can finish.
        if run.key is None or run.published or not run.delete_key(run.key):
            raise
        raise AdminError(run.stopped(str(e).split(". Changed so far")[0])) from None
    except KeyboardInterrupt:
        why = "Stopped by Ctrl-C"
        # The team file wasn't there before (check_out), so one there now may hold the key.
        if run.published or os.path.lexists(run.team.team_file):
            why += f" after the team file was written, at {run.team.team_file}"
        elif run.key is not None:
            run.delete_key(run.key)
        elif run.key_asked:
            why += " while gcloud made the key: check the account's keys in the Cloud console"
        raise Cancelled(run.stopped(why)) from None
    grants = sum(len(g) for g in team.grants().values())
    say(
        f"Done: {0 if account_there else 1} account made, "
        f"{len(team.grants()) - len(present)} folders made, {grants} grants, 1 key."
    )
    say(f"Team file: {path}")
    for line in next_steps(team, path, optout_link(team)):
        say(line)
    return 0


# --- diffdata admin optouts (#132, decision 6, PR 3) -----------------------------------------


def list_url(bucket: str, team: str) -> str:
    return f"gs://{bucket}/{team}/{team_optouts.LIST_NAME}"


def read_ids(ask: Callable[[str], str], say: Callable[[str], None], known: list[str]) -> list[str]:
    """The IDs the project team found in the form's answers, typed or pasted one per line.
    "none" on the first line means nobody new; an empty line ends the list. Never from a
    file or the command line, so they don't stay in the shell's history."""
    say(
        "Paste the Slack user IDs of the people who opted out of everything, one per line, then "
        "an empty line. Type none if nobody new did. People who opted out of particular uses "
        "only don't go on this list: record them in the private log."
    )
    found: list[str] = []
    while True:
        line = ask("> ").strip()
        if not found and line.lower() == "none":
            return []
        if not line:
            if found:
                return found
            say("Type the IDs, or none.")
            continue
        if not USER_ID.fullmatch(line):
            say("That isn't a Slack user ID (U or W, then 8 or more capitals and digits). Again:")
        elif line in found or line in known:
            say("That one is in the list already.")
        else:
            found.append(line)


def optouts(
    team: str,
    bucket: str,
    ask: Callable[[str], str],
    ask_yes: Callable[[str], bool],
    say: Callable[[str], None] = print,
    gcloud: Runner = run_gcloud,
) -> int:
    """Write the team's opted-out list to <team>/_config/opted-out.json, after showing the
    team and the count and getting a yes (item 9). An existing list is only added to, and the
    write is refused if the list changed meanwhile."""
    if WINDOWS:
        raise AdminError(f"Run admin on macOS or Linux. {NOTHING_CHANGED}")
    if not push.bucket_name(bucket):
        raise AdminError(f"--bucket isn't a Google Cloud Storage bucket name. {NOTHING_CHANGED}")
    if not push.SEGMENT.fullmatch(team):
        raise AdminError(f"The team name must be letters, digits, - and _. {NOTHING_CHANGED}")
    run = AddTeam(Team(team, bucket, "", "", (), Path()), gcloud=gcloud)
    config_folder = f"gs://{bucket}/{team}/{CONFIG}/"
    if (
        run.exists("storage", "managed-folders", "describe", config_folder, what=config_folder)
        is None
    ):
        raise AdminError(
            f"{config_folder} isn't there. Run add-team for this team first. {NOTHING_CHANGED}"
        )
    url = list_url(bucket, team)
    described = run.exists("storage", "objects", "describe", url, what="the team's list")
    known: list[str] = []
    generation = "0"  # 0: write only if there's no list yet
    if described is not None:
        generation = str(described.get("generation", ""))
        if not generation.isdigit():
            raise AdminError(f"gcloud didn't give the list's version. {NOTHING_CHANGED}")
        raw = run.call("storage", "cat", f"{url}#{generation}", what="Reading the list").stdout
        try:
            known = team_optouts.parse_list((raw or "").encode("utf-8"), team)
        except team_optouts.ListError:
            raise AdminError(
                f"The list at {url} isn't one admin wrote for {team}. Look at it in the console. "
                f"{NOTHING_CHANGED}"
            ) from None
    say(f"Team {team}: {len(known)} person(s) on its opt-out list now.")
    new = read_ids(ask, say, known)
    say(f"Team {team}, {url}:")
    say(f"  {len(known)} already on the list, {len(new)} to add, {len(known) + len(new)} in all:")
    for user_id in new:
        say(f"  add {user_id}")
    if not ask_yes("Have you looked at the form's answers, and is this list right?"):
        say(NOTHING_CHANGED)
        return 1
    body = {
        "format": team_optouts.LIST_FORMAT,
        "version": team_optouts.LIST_VERSION,
        "team": team,
        "slack_opted_out": known + new,
    }
    with tempfile.TemporaryDirectory(prefix=".diffdata-optouts-") as temp:
        os.chmod(temp, 0o700)
        path = Path(temp) / "opted-out.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
            f.write("\n")
        result = gcloud(
            [
                "storage", "cp", str(path), url, f"--if-generation-match={generation}",
                "--content-type=application/json", "--quiet",
            ]
        )  # fmt: skip
    if result.returncode != 0:
        if re.search(r"\b412\b|precondition", result.stderr or "", re.IGNORECASE):
            raise AdminError(
                f"The list changed while you typed. Run again to see it. {NOTHING_CHANGED}"
            )
        raise AdminError(f"Writing the list failed: {last_line(result.stderr)}. {NOTHING_CHANGED}")
    say(f"Done: {len(known) + len(new)} person(s) on {team}'s opt-out list, {len(new)} added.")
    say("The producer's next export fetches it.")
    return 0
