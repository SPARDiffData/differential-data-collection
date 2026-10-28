"""`diffdata consent review` and `diffdata consent withdraw` (CON-1, CON-3).

A person, typing in their own terminal, approves or skips each new canvas and Google file
the reference log found, or withdraws an approval. The list, with its titles and links,
shows only in that terminal. Both commands refuse unless their input and their output are a
terminal, so Claude, which runs commands with neither, can't answer. On Windows, Python's
isatty() is also true for the null device, which is where Claude Code's tools point a
command's input, so Windows is asked whether the input is a real console.

Review lists what the next build would mark new (discover/references.py): canvases and
Google files with a current link from a place still opted in, and no decision yet.
Attachments and ordinary links are never on it. The answer is one of:
  all            approve them all
  all but b, d   approve all but those letters, which are skipped
  none           skip them all
An empty answer, closed input or Ctrl+C records nothing, and the items stay new.

Withdraw lists what was approved at review. The answer is "all", "none", or letters such as
"b, d". It takes effect from the next export run.

Both record nothing, and say why, when the approvals file can't be read or would be written
somewhere unsafe (consent/approvals.py), when the answer isn't one they take, or when the
reference log, the approvals or config.toml changed while the list was shown. Their error
messages hold no titles, links or IDs, since Claude may see them.
"""

import hashlib
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from diffdata.common.config import Config
from diffdata.consent import approvals
from diffdata.consent.approvals import ApprovalsError
from diffdata.discover import references
from diffdata.discover.slack_inbox import run_channel_names

KINDS = {
    references.CANVAS: "Slack canvas",
    "google-doc": "Google Doc",
    "google-sheet": "Google Sheet",
    "google-slides": "Google Slides",
    references.GOOGLE_FILE: "Google Drive file",
}
# Letters as typed, after spaces are tidied: "b, d", "b,d" or "b d".
LETTERS = re.compile(r"[a-z]+(?:(?: ?, ?| )[a-z]+)*")
# Titles are cut at this many characters. Links aren't, so the real link always shows.
TITLE_LIMIT = 80

NOT_A_TERMINAL = (
    "diffdata consent {step} only runs in a terminal you type in, so nothing was recorded. "
    "Open a terminal, such as PowerShell, in the diffdata folder, and run: "
    "uv run diffdata consent {step}"
)
UNREADABLE = (
    "Couldn't read the approvals file next to config.toml, so nothing was shown or recorded, "
    "and no earlier decision is lost. A line in it may be damaged."
)
LOG_UNREADABLE = (
    "Couldn't read the reference log, so nothing was shown or recorded. The next "
    "/slack-export's build says whether it can read it."
)
CHANGED = (
    "Something changed while the list was shown, such as a build, another review or a "
    "withdrawal, so nothing was recorded. Run this again to see the list as it is now."
)
NO_ANSWER = "No answer, so nothing was recorded."

REVIEW_INTRO = """\
{count} new canvas(es) and Google file(s) were found in your saved Slack data.

What approving means:
  - diffdata reads an approved item on every /slack-export from now on, and keeps every
    version of it.
  - Claude reads an approved canvas in full when it collects it. An approved Google file
    waits for the Google Docs fetcher.
  - Approve only what your team agreed to share. A doc others wrote may need their
    agreement too (CON-5).
  - A skipped item isn't asked about again. To withdraw an approval later, run:
    uv run diffdata consent withdraw
"""
REVIEW_ANSWERS = """
Type one of these, then press Enter:
  all            approve them all
  all but b, d   approve all but the letters you give, and skip those
  none           skip them all
Press Enter alone, or close the window, to decide later: nothing is recorded."""
BAD_REVIEW_ANSWER = (
    "That isn't an answer review takes, so nothing was recorded. Type all, none, or all but "
    "and the letters to skip, such as: all but b, d"
)

WITHDRAW_INTRO = """\
{count} item(s) were approved at review.

Withdrawing one stops diffdata reading it from the next /slack-export. An export that's
already open keeps its list until it ends. What was already collected stays in your data
folder for now. A canvas listed in config.toml is withdrawn by removing it from
slack_canvases there.
"""
WITHDRAW_ANSWERS = """
Type one of these, then press Enter:
  all            withdraw them all
  b, d           withdraw only these letters
  none           withdraw nothing
Press Enter alone, or close the window, to leave them as they are."""
BAD_WITHDRAW_ANSWER = (
    "That isn't an answer withdraw takes, so nothing was recorded. Type all, none, or the "
    "letters to withdraw, such as: b, d"
)


def is_terminal(stream) -> bool:
    """Whether `stream` is a terminal a person types in or reads: not a pipe, a file or the
    null device."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if not os.isatty(fd):
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return False
    mode = ctypes.c_ulong()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def in_terminal(stdin, stdout) -> bool:
    """Whether a person can read the list and type the answer. A program can still fake a
    terminal, such as with `script` on macOS and Linux: the repo's .claude/settings.json
    also stops Claude's shell tools running these commands."""
    return is_terminal(stdin) and is_terminal(stdout)


def clean(text, limit: int | None = None) -> str:
    """One line with no control or format characters, so a crafted title or link can't start
    a new line, move the cursor, or reorder what's shown, as a right-to-left mark can. With
    a limit, a longer text is cut short, so it can't wrap onto a line of its own."""
    shown = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(text))
    shown = " ".join(shown.split())
    return shown if limit is None or len(shown) <= limit else shown[: limit - 1] + "…"


def letters(count: int) -> list[str]:
    """a to z, then aa, ab and so on."""
    found = []
    for number in range(1, count + 1):
        letter = ""
        while number:
            number, rest = divmod(number - 1, 26)
            letter = chr(ord("a") + rest) + letter
        found.append(letter)
    return found


def describe(entry: dict, channels: dict[str, str], titles: dict[str, str]) -> str:
    """One item, on one line: its type, its real link, its title, and when and where it was
    first referenced."""
    link = clean(entry["link"]) if entry["link"] else ""
    parts = [
        KINDS.get(entry["type"], "Source"),
        link or f"ID {clean(entry['key'].partition(':')[2])}, with no link in the saved data",
    ]
    if entry["title"] and clean(entry["title"]) != link:
        parts.append(f'"{clean(entry["title"], TITLE_LIMIT)}"')
    places = sorted(entry["places"].values(), key=lambda p: (p["at"], p["place"]))
    if places:
        first = places[0]
        if first["canvas_id"]:
            title = titles.get(first["canvas_id"], first["canvas_id"])
            where = f'canvas "{clean(title, TITLE_LIMIT)}"'
        else:
            where = "#" + channels.get(first["channel_id"], first["channel_id"] or "")
        parts.append(f"first referenced {clean(first['at'][:10])} in {clean(where)}")
    return " · ".join(parts)


def canvas_titles(log: dict[str, dict]) -> dict[str, str]:
    return {
        key.removeprefix("slack-file:"): entry["title"]
        for key, entry in log.items()
        if entry["type"] == references.CANVAS and entry["title"]
    }


def fingerprint(config: Config, path: Path) -> bytes:
    """What the list and the answer depend on, as they are on disk: the reference log, the
    approvals and config.toml."""
    digest = hashlib.sha256()
    for file in (references.log_dir(config.data_dir) / "references.jsonl", path, config.path):
        try:
            body = file.read_bytes()
        except FileNotFoundError:
            body = b""
        except OSError as e:
            raise ApprovalsError(
                "Couldn't read the files the list comes from, so nothing was recorded."
            ) from e
        digest.update(hashlib.sha256(body).digest())
    return digest.digest()


def load(config: Config, path: Path):
    """The fingerprint, the decisions so far, the log with the states the next build would
    set (None if diffdata can't read it) and the channel names."""
    seen = fingerprint(config, path)
    decisions = approvals.read_decisions(path)
    if decisions is None:
        raise ApprovalsError(UNREADABLE)
    channels = run_channel_names(config.data_dir)
    return seen, decisions, references.log_now(config, decisions, channels), channels


def waiting(log: dict[str, dict]) -> list[dict]:
    """The sources the next build would mark new, oldest first. A key the approvals file
    doesn't take, such as a canvas ID too short to be real, is left out, so answering can't
    write a line diffdata can't read back."""
    found = [
        entry
        for entry in log.values()
        if entry["state"] == references.NEW and approvals.KEY.fullmatch(entry["key"])
    ]
    return sorted(found, key=lambda entry: (entry["first_seen"], entry["key"]))


def ask(stdin, stdout) -> str:
    """The answer, in lowercase with its spaces tidied: "" for an empty line, closed input
    or Ctrl+C."""
    stdout.write("Your answer: ")
    stdout.flush()
    try:
        line = stdin.readline()
    except (KeyboardInterrupt, EOFError):
        stdout.write("\n")
        return ""
    return " ".join(line.lower().split())


def chosen(text: str, names: list[str]) -> set[str] | None:
    """The letters in `text`, or None if it isn't letters from the list."""
    if not LETTERS.fullmatch(text):
        return None
    picked = set(re.split(r"[ ,]+", text))
    return picked if picked <= set(names) else None


def review_answer(answer: str, names: list[str]) -> set[str] | None:
    """The letters to skip, or None if the answer isn't one review takes."""
    if answer == "all":
        return set()
    if answer == "none":
        return set(names)
    if answer.startswith("all but "):
        return chosen(answer.removeprefix("all but "), names)
    return None


def withdraw_answer(answer: str, names: list[str]) -> set[str] | None:
    """The letters to withdraw, or None if the answer isn't one withdraw takes."""
    if answer == "all":
        return set(names)
    if answer == "none":
        return set()
    return chosen(answer, names)


def show(stdout, text: str) -> None:
    print(text, file=stdout)


def refuse(message: str) -> int:
    print(f"diffdata: {message}", file=sys.stderr)
    return 1


def run_review(config: Config, stdin=None, stdout=None, now: datetime | None = None) -> int:
    """Show the sources waiting for review, then record the answer. Returns the exit code."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not in_terminal(stdin, stdout):
        return refuse(NOT_A_TERMINAL.format(step="review"))
    try:
        path = approvals.safe_file(config)
        seen, _, log, channels = load(config, path)
        if log is None:
            raise ApprovalsError(LOG_UNREADABLE)
        items = waiting(log)
        if not items:
            show(stdout, "Nothing is waiting for review.")
            return 0
        names = letters(len(items))
        titles = canvas_titles(log)
        show(stdout, REVIEW_INTRO.format(count=len(items)))
        for name, entry in zip(names, items, strict=True):
            show(stdout, f"{name}) {describe(entry, channels, titles)}")
        show(stdout, REVIEW_ANSWERS)
        answer = ask(stdin, stdout)
        if not answer:
            show(stdout, f"{NO_ANSWER} These items stay new, and are on the next list.")
            return 0
        skipped = review_answer(answer, names)
        if skipped is None:
            show(stdout, BAD_REVIEW_ANSWER)
            return 1
        keys = [entry["key"] for entry in items]
        decided = [
            (key, approvals.SKIPPED if name in skipped else approvals.APPROVED)
            for name, key in zip(names, keys, strict=True)
        ]
        with approvals.locked(path):  # the last check and the write, with nothing in between
            again, _, log, _ = load(config, path)
            if again != seen or log is None or [entry["key"] for entry in waiting(log)] != keys:
                raise ApprovalsError(CHANGED)
            approvals.record(path, decided, answer, now or datetime.now(UTC))
    except ApprovalsError as e:
        return refuse(str(e))
    approved = len(items) - len(skipped)
    show(stdout, f"Recorded: {approved} approved, {len(skipped)} skipped.")
    if approved:
        show(
            stdout,
            "The next /slack-export reads the approved canvases. Approved Google files wait "
            "for the Google Docs fetcher.",
        )
    return 0


def run_withdraw(config: Config, stdin=None, stdout=None, now: datetime | None = None) -> int:
    """Show what was approved at review, then record the withdrawals. Returns the exit code."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not in_terminal(stdin, stdout):
        return refuse(NOT_A_TERMINAL.format(step="withdraw"))
    try:
        path = approvals.safe_file(config)
        seen, decisions, log, channels = load(config, path)
        log = log or {}  # the log only describes the items, so withdrawing doesn't need it
        approved = sorted(
            key for key, decision in decisions.items() if decision == approvals.APPROVED
        )
        if not approved:
            show(
                stdout,
                "Nothing was approved at review, so there's nothing to withdraw here. A canvas "
                "listed in config.toml is withdrawn by removing it from slack_canvases there.",
            )
            return 0
        names = letters(len(approved))
        listed = {f"slack-file:{canvas}" for canvas in config.slack_canvases}
        titles = canvas_titles(log)
        show(stdout, WITHDRAW_INTRO.format(count=len(approved)))
        for name, key in zip(names, approved, strict=True):
            entry = log.get(key) or {
                "key": key,
                "type": references.CANVAS if key.startswith("slack-file:") else "google-file",
                "link": None,
                "title": None,
                "places": {},
            }
            also = " · also listed in config.toml" if key in listed else ""
            show(stdout, f"{name}) {describe(entry, channels, titles)}{also}")
        show(stdout, WITHDRAW_ANSWERS)
        answer = ask(stdin, stdout)
        if not answer:
            show(stdout, f"{NO_ANSWER} Nothing was withdrawn.")
            return 0
        picked = withdraw_answer(answer, names)
        if picked is None:
            show(stdout, BAD_WITHDRAW_ANSWER)
            return 1
        if not picked:
            show(stdout, "Nothing was withdrawn.")
            return 0
        withdrawn = [
            (name, key) for name, key in zip(names, approved, strict=True) if name in picked
        ]
        decided = [(key, approvals.WITHDRAWN) for _, key in withdrawn]
        with approvals.locked(path):  # the last check and the write, with nothing in between
            if load(config, path)[0] != seen:
                raise ApprovalsError(CHANGED)
            approvals.record(path, decided, answer, now or datetime.now(UTC))
    except ApprovalsError as e:
        return refuse(str(e))
    show(
        stdout,
        f"Withdrawn: {len(withdrawn)}. diffdata stops reading them from the next /slack-export.",
    )
    if also_listed := [name for name, key in withdrawn if key in listed]:
        show(
            stdout,
            f"Also listed in slack_canvases in config.toml: {', '.join(also_listed)}. That's an "
            "opt-in of its own, so remove them there too, or they'll still be read.",
        )
    return 0
