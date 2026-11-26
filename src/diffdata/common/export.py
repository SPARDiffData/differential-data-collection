"""`diffdata export` (#128, revision 2): after day 7, one command does the rest.

It runs in the producer's own terminal, in this order:
  1  checks: setup is done, every channel's 7 days are over, and the team's opt-out list
     can be fetched. Nothing is read from Slack before these pass.
  2  starts Claude Code with `/slack-export test`, then, after a y, with `/slack-export`
  3  starts the consent review (consent/review.py) by itself
  4  more rounds by themselves: `/slack-export` again for newly approved canvases, and the
     Google sign-in and fetch for the docs listed or approved, each followed by the review.
     It stops when nothing new waits, after 3 rounds, or when the producer types stop
  5  rebuilds if the opt-out list changed since the build, then scrubs
  6  asks for a typed `push` once the producer has read the copies, and pushes this scrub only
  7  runs clean-history, after the Claude Code it started has ended

Claude Code opens interactively, with only the slash command as its prompt, in the repo
folder, so the repo's rules, hooks and deny list apply. The producer types /exit when it's
done. Whether a step worked is read from the files the export writes (the closed run in
slack/runs/), not from what Claude says. Headless `claude -p` would need a permission flag
for the command's tools, which must-refuse 4 rules out.

Run again after stopping, it starts at the first unfinished step, worked out from files the
tool already writes: an export built after the last complete push (slack/runs/ and pushes/)
means steps 1 and 2 are done. The review, the scrub and the push question always run again.

What it refuses (#128, must-refuse list, approved on 2026-11-26):
  1  to run unless its input and output are a terminal a person types in, or when CLAUDECODE
     is set. .claude/settings.json also denies it to Claude's shell tools
  2  to answer the review: it runs review's own code, with its own terminal check. There's
     no --yes
  3  to push without a `push` typed for this scrub: push() checks the manifest it was typed
     for, under the scrub's lock, and every copy against it
  4  to give Claude more than the slash command: no permission flag, ever
  5  to start Claude before day 7 or without the team's opt-out list
  6  to clear history while the Claude Code it started still runs: each run is waited for
  (7 is the installer's, in PR A.)
  8  to print anything private: every step prints counts, and paths on this computer
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from diffdata.collect import google_fetch, google_signin
from diffdata.common import check, history
from diffdata.common.config import ConfigError, channel_id, load_config
from diffdata.common.paths import UnsafeDataDirError
from diffdata.consent import approvals, review
from diffdata.consent.approvals import ApprovalsError
from diffdata.discover import references, slack_export, slack_inbox, team_optouts
from diffdata.scrub import run as scrub_run
from diffdata.store import push as store_push
from diffdata.store.backend import StoreError

try:
    import termios  # macOS and Linux
except ImportError:  # Windows
    termios = None

REPO = history.REPO
ROUNDS = 3
NOT_A_TERMINAL = (
    "run `uv run diffdata export` yourself, in your own terminal, with Claude Code closed. "
    "It asks for your answers along the way, so only a person typing may run it."
)
AGAIN = "Run uv run diffdata export again to carry on: your review answers so far are kept."
NO_CLAUDE = (
    "Claude Code wasn't found: neither the `claude` command nor the Claude desktop app's copy. "
    "Open Claude Code in this folder and type /slack-export test, then /slack-export. Then run "
    "uv run diffdata export again."
)
ERRORS = (
    ConfigError,
    UnsafeDataDirError,
    ApprovalsError,
    slack_inbox.SlackExportError,
    team_optouts.ListError,
    google_signin.SignInError,
    scrub_run.ScrubError,
    store_push.PushError,
    StoreError,
)

Ask = Callable[[str], str]
Say = Callable[[str], None]
Claude = Callable[[list[str]], object]


class Stop(Exception):
    """The export stops here. The message says why and what to do."""


def by_a_person() -> bool:
    """A terminal a person types in, and not a Claude Code shell (must-refuse 1)."""
    return review.in_terminal(sys.stdin, sys.stdout) and not history.run_by_claude()


def claude_command() -> list[str] | None:
    """The `claude` command, else the Claude desktop app's newest Claude Code."""
    found = shutil.which("claude")
    if found and "windowsapps" not in found.lower():  # the Windows alias opens the app
        return [found]
    versions = check.desktop_versions()
    for version in sorted(versions, reverse=True):
        patterns = ("*/claude.app/Contents/MacOS/claude", "claude.exe", "*/claude.exe", "*/claude")
        for pattern in patterns:
            for path in sorted(versions[version].glob(pattern)):
                if path.is_file():
                    return [str(path)]
    return None


def start_claude(argv: list[str]) -> None:
    """Claude Code, in this terminal and the repo folder, until the producer types /exit."""
    subprocess.run(argv, cwd=REPO, check=False)


def runs(data: Path) -> list[dict]:
    """Every closed export run."""
    found = []
    for path in sorted((slack_inbox.slack_dir(data) / "runs").glob("*.json")):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(run, dict):
            found.append(run)
    return found


def built_since(data: Path, started: str, test: bool) -> bool:
    """Whether a test run, or an export, opened after `started`, saved results through the
    hook, and was built. A build with nothing saved means the hook never ran."""
    inbox = slack_inbox.inbox_dir(data)
    return any(
        run.get("closed_why") == "built"
        and str(run.get("started_at", "")) >= started
        and run.get("test") is test
        and any(inbox.glob(f"*/{run.get('run_id')}/*.json"))
        for run in runs(data)
    )


def exported_since_push(data: Path) -> bool:
    """Whether an export was built after the last push that sent everything, its record
    included: then steps 1 and 2 are done, and a run again starts at the review. A run from
    before runs said whether they were tests doesn't count, so a new export is read."""
    pushed = []
    for path in (data / store_push.LOCAL_PUSHES).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(record, dict)
            and record.get("complete") is True
            and record.get("record_sent", True) is True
        ):
            pushed.append(str(record.get("pushed_at", "")))
    built = [
        str(run.get("closed_at", ""))
        for run in runs(data)
        if run.get("closed_why") == "built" and run.get("test") is False
    ]
    return max(built, default="") > max(pushed, default="")


def fresh(ask: Ask, question: str) -> str:
    """The answer, typed after the question shows: anything pasted ahead is dropped first."""
    drop_typed_ahead()
    return ask(question).strip().lower()


def drop_typed_ahead() -> None:
    """Drop anything typed or pasted before a question shows, so it can't answer it."""
    if termios is not None:
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (OSError, ValueError, termios.error):
            pass
    elif sys.platform == "win32":
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()


def yes(ask: Ask, say: Say, question: str) -> bool:
    while True:
        answer = fresh(ask, f"{question} [y/n] ")
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        say("Please type y or n.")


def slack(claude: Claude, command: list[str], test: bool, data: Path, say: Say) -> None:
    """One /slack-export in Claude Code. Stops unless its run was built."""
    prompt = "/slack-export test" if test else "/slack-export"
    say(
        f"\nClaude Code opens now and runs {prompt}. If it waits with {prompt} typed, press "
        "Enter. When it has finished, type /exit to come back here."
    )
    started = slack_inbox.utc_iso(datetime.now(UTC))
    preflight(lambda _: None)  # again: config.toml may have changed while a question waited
    claude([*command, prompt])  # waited for: Claude has ended when this returns (item 6)
    if not built_since(data, started, test):
        what = "The test run" if test else "The export"
        raise Stop(
            f"{what} didn't finish, so the export stops here. Claude's last message says why."
        )


def ask_review(config, ask: Ask, say: Say) -> None:
    """The review, as `diffdata consent review` runs it, with its own terminal check."""
    drop_typed_ahead()
    while review.run_review(config) != 0:
        if not yes(ask, say, "The review didn't record anything. Try it again?"):
            raise Stop(f"Stopped. {AGAIN}")


def rounds(config, claude: Claude, command: list[str], ask: Ask, say: Say) -> None:
    """Steps 3 and 4: the review, then what it approved, up to ROUNDS times."""
    data = config.data_dir
    tried: set[str] = set()
    fetched: set[str] | None = None
    for number in range(ROUNDS + 1):
        say("\nThe review:")
        ask_review(config, ask, say)
        canvases = set(approvals.approved_canvases(config) or ())
        canvases -= references.canvases_read(data) | tried
        docs = {doc for doc, _ in google_fetch.to_fetch(config)[0]}
        new_docs = docs - (fetched or set())
        if not canvases and not new_docs:
            return
        if number == ROUNDS:
            say(f"That's {ROUNDS} rounds. Anything still waiting is collected on the next export.")
            return
        answer = fresh(
            ask,
            f"Next: collect {len(canvases)} approved canvas(es) and {len(new_docs)} Google "
            "Doc(s), then review again. Press Enter to go on, or type stop: ",
        )
        if answer == "stop":
            raise Stop(f"Stopped. {AGAIN}")
        if canvases:
            slack(claude, command, False, data, say)
            tried |= canvases
        if new_docs:
            got = references.google_docs_fetched(data)[0]
            if any(f"google:{doc}" not in got for doc in new_docs):
                say("Google's sign-in opens in your browser: pick the docs to share.")
                say(google_signin.sign_in(listed=sorted(docs)))
            try:
                say(google_fetch.collect(config))
            except google_signin.SignInError:  # the saved sign-in is gone or ran out: once more
                say("Google's sign-in opens in your browser: pick the docs to share.")
                say(google_signin.sign_in(listed=sorted(docs)))
                say(google_fetch.collect(config))
            fetched = docs


def preflight(say: Say):
    """Step 1, before anything is read (item 5): setup is done, every channel's 7 days are
    over, and the team's opt-out list is fetched. Returns config.toml read after the fetch,
    so the IDs just fetched are in slack_opted_out."""
    config = load_config()
    gaps = [item for item in check.state_items() if not item.ok]
    if gaps:
        raise Stop("\n".join(["Setup isn't done. Fix these, then run again:", *check.lines(gaps)]))
    shown = {
        (f"Channel {n}, listed by its ID," if channel_id(key) else f"#{key}"): notice
        for n, (key, notice) in enumerate(config.slack_notices, start=1)
    }
    closes = team_optouts.check_windows(shown, datetime.now(UTC))
    say(team_optouts.fetch_list(config, closes))
    return load_config()


def steps(ask: Ask, say: Say, claude: Claude) -> int:
    config = preflight(say)
    data = config.data_dir
    command = claude_command()
    if command is None:
        raise Stop(NO_CLAUDE)
    # 2: the test run, then the export.
    if exported_since_push(data):
        say("Your export is done but not pushed yet, so this carries on from the review.")
    else:
        slack(claude, command, True, data, say)
        if not yes(ask, say, "\nThe test run worked. Export now?"):
            say(f"Stopped. Nothing more was read. {AGAIN}")
            return 0
        slack(claude, command, False, data, say)
    rounds(config, claude, command, ask, say)
    # 5: scrub, rebuilt first if the opt-out list changed since the build. Read again: each
    # /slack-export's start fetches the team's list too.
    config = load_config()
    if scrub_run.saved_left_out(config)[2] != slack_inbox.applied_stamp(data):
        say(slack_export.build(config))
    say("\nScrubbing:")
    say(scrub_run.scrub(config))
    say(
        f"Read the copies in {data / scrub_run.SCRUBBED} before you push, and what to check in "
        f"{data / scrub_run.PRIVATE / scrub_run.FLAGS}."
    )
    # 6: push, after a push typed for this scrub (item 3).
    answered = store_push.manifest_digest(data)
    if answered is None:
        raise Stop(f"The scrub left no manifest, so nothing was sent. {AGAIN}")
    if fresh(ask, "Have you read the copies? Type push to upload them, or Enter to stop: ") != (
        "push"
    ):
        say(f"Nothing was sent. {AGAIN}")
        return 0
    # Read again: an opt-out added while the question waited makes push refuse these copies.
    report = store_push.push(load_config(), answered=answered)
    say(report)
    # 7: clean-history. Every Claude Code this started has ended (item 6).
    if getattr(report, "complete", False):
        drop_typed_ahead()
        return history.clean(ask, say, data=data)
    say(history.LATER)
    return 0


def run(ask: Ask = input, say: Say = print, claude: Claude = start_claude) -> int:
    """The whole of `diffdata export`. Returns the exit code."""
    if not by_a_person():
        say(f"diffdata export: {NOT_A_TERMINAL}")
        return 1
    try:
        return steps(ask, say, claude)
    except Stop as e:
        say(f"diffdata export: {e}")
        return 1
    except ERRORS as e:
        say(f"diffdata export: {e}")
        return 1
    except (KeyboardInterrupt, EOFError):
        say(f"\ndiffdata export: stopped. {AGAIN}")
        return 130
    except Exception as e:
        # Its message or a traceback could quote collected text, so only its type is shown.
        say(f"diffdata export: stopped on an unexpected {type(e).__name__}. {AGAIN}")
        return 1
