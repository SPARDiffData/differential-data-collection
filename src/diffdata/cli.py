"""The `diffdata` command.

`setup` (ENG-1, CON-1, STO-1), `check` (ENG-1), `clean-history` (CON-5, #150), `admin
add-team` and `optouts` (STO-1, CON-4, for the project team), `discover slack` (DIS-1),
`consent` (CON-1, CON-3), `collect google-docs` (COL-4, COL-5), `scrub` (SCR-1, SCR-2) and
`push` (STO-1, STO-2, STO-3) are built.
Any other subcommand added before it's built is a stub that names the requirement IDs
(docs/requirements.md) its feature Issue will build.
"""

import argparse
import sys
from pathlib import Path

from diffdata import __version__
from diffdata.collect import google_fetch, google_signin
from diffdata.common import check as producer_check
from diffdata.common import history
from diffdata.common import setup as producer_setup
from diffdata.common.config import ConfigError, load_config, user_config_file
from diffdata.common.paths import UnsafeDataDirError
from diffdata.consent import review
from diffdata.discover import slack_export, slack_inbox
from diffdata.scrub import run as scrub_run
from diffdata.store import admin as store_admin
from diffdata.store import push as store_push
from diffdata.store.backend import StoreError

try:
    import termios  # macOS and Linux, where admin runs
except ImportError:  # Windows
    termios = None

# subcommand -> (help text, requirement IDs it will implement)
COMMANDS = {
    "setup": (
        "Answer a few questions in your own terminal, and diffdata writes config.toml for you.",
        "ENG-1, CON-1, STO-1",
    ),
    "check": (
        "Check what you need is installed, and that setup is done. Changes nothing.",
        "ENG-1",
    ),
    "admin": (
        "For the project team: set up a pilot team's folder, upload key and team file, and "
        "write its opt-out list on day 7.",
        "STO-1",
    ),
    "discover": ("Find Slack messages and the sources they reference.", "DIS-1, DIS-2, DIS-3"),
    "consent": (
        "Approve, skip or withdraw the canvases and Google files found, in your own terminal.",
        "CON-1, CON-3",
    ),
    "collect": ("Fetch each referenced source we can access.", "COL-1, COL-2, COL-4, COL-7"),
    "scrub": (
        "Make scrubbed copies, with secrets, meeting links, phone numbers, emails, Slack IDs and "
        "people replaced.",
        "SCR-1, SCR-2",
    ),
    "push": (
        "Send the scrubbed copies the last scrub passed to your team's folder in the store.",
        "STO-1, STO-2, STO-3",
    ),
    "clean-history": (
        "Delete this repo's Claude Code session history, after collecting, in your own terminal "
        "with Claude Code closed. It shows what it will delete and asks first.",
        "CON-5",
    ),
}
STUBS: tuple[str, ...] = ()

SLACK_STEPS = {
    "channels": "List the opted-in channels (names or IDs) from config.toml, and its path.",
    "begin": "Open an export run for channels given as name=ID or ID (run by /slack-export).",
    "check": "Stop a Slack read the open run doesn't name (run by the /slack-export hook).",
    "save": "Save one Slack result from stdin to the inbox (run by the /slack-export hook).",
    "canvases-due": "List the canvases this run should read, and why (run by /slack-export).",
    "build": "Build the message store and the markdown view from the inbox, then close the run.",
    "abort": "Close the open export run without building.",
}
# Typed by a person in a terminal: both refuse to run anywhere else, as in Claude's tools.
CONSENT_STEPS = {
    "review": "Approve or skip the new canvases and Google files the reference log found.",
    "withdraw": "Withdraw approvals given at review, from the next /slack-export.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffdata",
        description="Collect research working data and ready it for storage.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")
    for name, (help_text, _) in COMMANDS.items():
        command = subcommands.add_parser(name, help=help_text, description=help_text)
        if name == "discover":
            add_discover_sources(command)
        if name == "consent":
            steps = command.add_subparsers(dest="step", metavar="<step>", required=True)
            for step, step_help in CONSENT_STEPS.items():
                steps.add_parser(step, help=step_help, description=step_help)
        if name == "collect":
            add_collect_sources(command)
        if name == "admin":
            add_admin_steps(command)
        if name == "setup":
            team_help = "The team file the project team sent you (<team>.diffdata-team.json)."
            command.add_argument("team_file", nargs="?", metavar="TEAM_FILE", help=team_help)
    return parser


def add_admin_steps(admin: argparse.ArgumentParser) -> None:
    steps = admin.add_subparsers(dest="step", metavar="<step>", required=True)
    add_help = (
        "Make a team's managed folder, its upload account and key, and the producers' read "
        "access, then write <team>.diffdata-team.json (#132). Run signed in to gcloud as the "
        "Cloud project's owner."
    )
    add = steps.add_parser("add-team", help=add_help, description=add_help)
    add.add_argument("team", metavar="TEAM", help="The team's folder name: letters, digits, - _.")
    add.add_argument("--bucket", required=True, help="The pilot's bucket.")
    add.add_argument("--project", required=True, help="The pilot's short project name.")
    add.add_argument("--gcp-project", required=True, help="The Google Cloud project ID.")
    add.add_argument(
        "--producer",
        action="append",
        default=[],
        metavar="EMAIL",
        help="A producer's Google account, given read access. Repeat for each producer.",
    )
    add.add_argument(
        "--out",
        default=store_admin.DEFAULT_OUT,
        help=f"Where to write the team file (default {store_admin.DEFAULT_OUT}).",
    )
    add.add_argument(
        "--optout-form",
        metavar="LINK",
        help="The shared opt-out form's pre-filled link, ending in the team field's '='.",
    )
    add.add_argument(
        "--google-client",
        metavar="PATH",
        help="The Desktop app client file, to put in the team file.",
    )
    optouts_help = (
        "On day 7, after looking at the opt-out form's answers: write the team's opted-out "
        "Slack IDs to its folder, where the producer's export fetches them (#132). You paste "
        "the IDs when asked."
    )
    optouts = steps.add_parser("optouts", help=optouts_help, description=optouts_help)
    optouts.add_argument("team", metavar="TEAM", help="The team's folder name.")
    optouts.add_argument("--bucket", required=True, help="The pilot's bucket.")


def add_collect_sources(collect: argparse.ArgumentParser) -> None:
    sources = collect.add_subparsers(dest="source", metavar="<source>", required=True)
    docs_help = (
        "Fetch and build the Google Docs listed in config.toml or approved at review, with "
        "your own Google login (COL-4, COL-5)."
    )
    docs = sources.add_parser("google-docs", help=docs_help, description=docs_help)
    steps = docs.add_subparsers(dest="step", metavar="<step>")
    sign_in_help = (
        "Sign in with your Google account and pick the docs to share, in Google's own "
        "picker. Run it again to pick more."
    )
    steps.add_parser("sign-in", help=sign_in_help, description=sign_in_help)
    build_help = "Rebuild the fetched docs without fetching anything."
    steps.add_parser("build", help=build_help, description=build_help)


def add_discover_sources(discover: argparse.ArgumentParser) -> None:
    sources = discover.add_subparsers(dest="source", metavar="<source>", required=True)
    slack_help = "Slack channels, read through Claude's Slack connector by /slack-export (DIS-1)."
    slack = sources.add_parser("slack", help=slack_help, description=slack_help)
    steps = slack.add_subparsers(dest="step", metavar="<step>", required=True)
    for name, help_text in SLACK_STEPS.items():
        step = steps.add_parser(name, help=help_text, description=help_text)
        if name == "begin":
            step.add_argument("channels", nargs="+", metavar="NAME=ID|ID")
            full_help = "Read every channel from the start, and every listed canvas."
            step.add_argument("--full", action="store_true", help=full_help)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "setup":
        return run_setup(args)
    if args.command == "check":
        return producer_check.check()
    if args.command == "admin":
        return run_admin(args)
    if args.command == "discover":
        return run_slack_step(args)
    if args.command == "consent":
        return run_consent_step(args)
    if args.command == "collect":
        return run_google_docs_step(args)
    if args.command == "scrub":
        return run_scrub()
    if args.command == "push":
        return run_push()
    if args.command == "clean-history":
        return run_clean_history()

    _, requirement_ids = COMMANDS[args.command]
    print(
        f"diffdata {args.command}: not built yet ({requirement_ids}). See docs/requirements.md.",
        file=sys.stderr,
    )
    return 1


def run_setup(args: argparse.Namespace) -> int:
    # First: setup writes the person's opt-in, so only a person typing may answer.
    if not review.in_terminal(sys.stdin, sys.stdout):
        print(f"diffdata: {producer_setup.NOT_A_TERMINAL}", file=sys.stderr)
        return 1
    # Checked first, shown at the end; a gap never stops setup (#136, must-refuse 3).
    found: list[producer_check.Item] = []
    for part in (producer_check.tool_items, lambda: [producer_check.repo_item()]):
        try:
            found += part()
        except Exception:
            pass
    try:
        team = Path(args.team_file) if args.team_file else None
        code = producer_setup.setup(team_file=team)
        for line in producer_check.before_export(found):
            print(line)
        return code
    except (producer_setup.SetupError, ConfigError, UnsafeDataDirError) as e:
        print(f"diffdata setup: {e}", file=sys.stderr)
        for line in producer_check.before_export(found):
            print(line)
        return 1
    except OSError as e:
        # setup() says what it wrote; this is the last net, with no traceback.
        print(f"diffdata setup: stopped: {e}", file=sys.stderr)
        for line in producer_check.before_export(found):
            print(line)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ndiffdata setup: stopped. A config.toml is changed only after your last yes.")
        return 130


def run_admin(args: argparse.Namespace) -> int:
    # It changes the Cloud project and makes a key, so only a person typing may answer.
    if not review.in_terminal(sys.stdin, sys.stdout):
        print(
            "diffdata admin: run this yourself, in your own terminal. It changes the Cloud "
            "project, and asks you before it does.",
            file=sys.stderr,
        )
        return 1
    try:
        if args.step == "optouts":
            prompter = producer_setup.Prompter()

            def fresh_yes(question: str) -> bool:
                # Drop anything pasted ahead, so a "y" in the paste can't answer (item 9).
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
                return prompter.yes(question)

            return store_admin.optouts(args.team, args.bucket, ask=prompter.ask, ask_yes=fresh_yes)
        client = args.google_client
        team = store_admin.Team(
            name=args.team,
            bucket=args.bucket,
            project=args.project,
            gcp_project=args.gcp_project,
            producers=tuple(args.producer),
            out=Path(args.out).expanduser(),
            optout_form=args.optout_form,
            google_client=store_admin.read_client(Path(client)) if client else None,
        )
        return store_admin.add_team(team, ask_yes=producer_setup.Prompter().yes)
    except store_admin.Cancelled as e:
        print(f"\ndiffdata admin: {e}", file=sys.stderr)
        return 130
    except store_admin.AdminError as e:
        print(f"diffdata admin: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ndiffdata admin: stopped. The lines above say what was done.")
        return 130


def run_consent_step(args: argparse.Namespace) -> int:
    # Before config.toml is read: its errors can quote its text and paths, and outside a
    # terminal nothing but the refusal may be shown.
    if not review.in_terminal(sys.stdin, sys.stdout):
        return review.refuse(review.NOT_A_TERMINAL.format(step=args.step))
    try:
        config = load_config()
    except (ConfigError, UnsafeDataDirError) as e:
        print(f"diffdata: {e}", file=sys.stderr)
        return 1
    if args.step == "review":
        return review.run_review(config)
    return review.run_withdraw(config)


def run_slack_step(args: argparse.Namespace) -> int:
    if args.step == "check":
        return slack_inbox.check_from_hook(sys.stdin.buffer.read())
    if args.step == "save":
        return slack_inbox.save_from_hook(sys.stdin.buffer.read())
    try:
        config = load_config()
        if args.step == "channels":
            # In config.toml's order: a test run reads only the first one.
            channels = list(config.listed_channels)
            if not channels:
                raise ConfigError(f"No channels are listed under slack_channels in {config.path}.")
            report = "\n".join([f"Opted-in channels, from {config.path}:", *channels])
        elif args.step == "begin":
            report = slack_inbox.begin(config, args.channels, full=args.full)
        elif args.step == "abort":
            report = slack_inbox.abort(config)
        elif args.step == "canvases-due":
            report = slack_export.canvases_due(config)
        else:
            report = slack_export.build(config)
    except (ConfigError, UnsafeDataDirError, slack_inbox.SlackExportError) as e:
        print(f"diffdata: {e}", file=sys.stderr)
        return 1
    print(report)
    return 0


def run_google_docs_step(args: argparse.Namespace) -> int:
    try:
        # First, so an unsafe data folder stops everything before signing in or fetching.
        config = load_config()
        if args.step == "sign-in":
            docs, _ = google_fetch.to_fetch(config)
            report = google_signin.sign_in(listed=[doc_id for doc_id, _ in docs])
        elif args.step == "build":
            report = google_fetch.build_report(config)
        else:
            report = google_fetch.collect(config)
    except (ConfigError, UnsafeDataDirError, google_signin.SignInError) as e:
        print(f"diffdata: {e}", file=sys.stderr)
        return 1
    print(report)
    return 0


def run_scrub() -> int:
    try:
        report = scrub_run.scrub(load_config())
    except (ConfigError, UnsafeDataDirError, scrub_run.ScrubError) as e:
        print(f"diffdata scrub: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Its message or a traceback could quote collected text, so only its type is shown.
        print(
            f"diffdata scrub: stopped on an unexpected {type(e).__name__}, before making any "
            "copies.",
            file=sys.stderr,
        )
        return 1
    print(report)
    return 0


def run_push() -> int:
    try:
        config = load_config()
        report = store_push.push(config)
    except (ConfigError, UnsafeDataDirError, store_push.PushError, StoreError) as e:
        print(f"diffdata push: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Its message or a traceback could quote collected text, so only its type is shown.
        print(f"diffdata push: stopped on an unexpected {type(e).__name__}.", file=sys.stderr)
        return 1
    print(report)
    if getattr(report, "complete", False):
        offer_clean_history(config.data_dir)
    else:
        print(history.LATER)
    return 0


def by_a_person() -> bool:
    """A terminal a person types in, and not a Claude Code shell (#150, must-refuse 4 and 9).
    history.clean() checks the same again itself."""
    return review.in_terminal(sys.stdin, sys.stdout) and not history.run_by_claude()


def history_data_dir() -> Path | None:
    """The data folder, so clean-history can protect it. None with no config.toml (then only
    ~/.diffdata is protected). A config.toml that's there but doesn't load stops it: the data
    folder isn't known (#150, must-refuse 7)."""
    # The same places load_config() looks: ~/.diffdata/, then the current folder.
    if not user_config_file().exists() and not (Path.cwd() / "config.toml").exists():
        return None
    try:
        return load_config().data_dir
    except Exception as e:
        raise history.HistoryError(
            f"Your config.toml doesn't load ({type(e).__name__}), so your data folder isn't known "
            "and clean-history won't run. Fix it (`uv run diffdata check` says what's wrong), "
            "then run clean-history again."
        ) from None


def offer_clean_history(data: Path) -> None:
    """After a push that sent everything (#150, D8): offer clean-history to a person, never to
    Claude. Otherwise, one line says how to run it later."""
    if not by_a_person():
        print(history.LATER)
        return
    try:
        history.offer(input, print, data=data)
    except (KeyboardInterrupt, EOFError):
        print(f"\nStopped. Some of the history may be deleted already. {history.LATER}")
    except OSError as e:
        print(f"diffdata clean-history: stopped: {type(e).__name__}. {history.LATER}")


def run_clean_history() -> int:
    if not by_a_person():
        print(f"diffdata clean-history: {history.NOT_A_TERMINAL}", file=sys.stderr)
        return 1
    try:
        return history.clean(input, print, data=history_data_dir())
    except history.HistoryError as e:
        print(f"diffdata clean-history: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "\ndiffdata clean-history: stopped. Some folders may be deleted already: run it again."
        )
        return 130
    except OSError as e:
        print(f"diffdata clean-history: stopped: {type(e).__name__}.", file=sys.stderr)
        return 1
