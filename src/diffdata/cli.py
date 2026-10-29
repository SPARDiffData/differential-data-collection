"""The `diffdata` command.

`discover slack` is built (DIS-1), and `collect google-docs sign-in` (COL-4). The other
subcommands are stubs that name the requirement IDs (docs/requirements.md) their feature
Issues will build.
"""

import argparse
import sys

from diffdata import __version__
from diffdata.collect import google_signin
from diffdata.common.config import ConfigError, load_config
from diffdata.common.paths import UnsafeDataDirError
from diffdata.discover import slack_export, slack_inbox

# subcommand -> (help text, requirement IDs it will implement)
COMMANDS = {
    "discover": ("Find Slack messages and the sources they reference.", "DIS-1, DIS-2, DIS-3"),
    "collect": ("Fetch each referenced source we can access.", "COL-1, COL-2, COL-4, COL-7"),
    "scrub": ("Redact secrets and personal information.", "SCR-1, SCR-2"),
    "push": ("Add scrubbed artifacts to central storage.", "STO-1, STO-2, STO-3"),
}
STUBS = ("scrub", "push")

SLACK_STEPS = {
    "channels": "List the opted-in channels from config.toml, and which file it read.",
    "begin": "Open an export run for channels given as name=ID (run by /slack-export).",
    "check": "Stop a Slack read the open run doesn't name (run by the /slack-export hook).",
    "save": "Save one Slack result from stdin to the inbox (run by the /slack-export hook).",
    "canvases-due": "List the canvases this run should read, and why (run by /slack-export).",
    "build": "Build the message store and the markdown view from the inbox, then close the run.",
    "abort": "Close the open export run without building.",
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
        if name == "collect":
            add_collect_sources(command)
    return parser


def add_collect_sources(collect: argparse.ArgumentParser) -> None:
    sources = collect.add_subparsers(dest="source", metavar="<source>", required=True)
    docs_help = "Google Docs, with the producer's own Google login (COL-4)."
    docs = sources.add_parser("google-docs", help=docs_help, description=docs_help)
    steps = docs.add_subparsers(dest="step", metavar="<step>", required=True)
    sign_in_help = (
        "Sign in with your Google account and pick the docs to share, in Google's own "
        "picker. Run it again to pick more."
    )
    steps.add_parser("sign-in", help=sign_in_help, description=sign_in_help)


def add_discover_sources(discover: argparse.ArgumentParser) -> None:
    sources = discover.add_subparsers(dest="source", metavar="<source>", required=True)
    slack_help = "Slack channels, read through Claude's Slack connector by /slack-export (DIS-1)."
    slack = sources.add_parser("slack", help=slack_help, description=slack_help)
    steps = slack.add_subparsers(dest="step", metavar="<step>", required=True)
    for name, help_text in SLACK_STEPS.items():
        step = steps.add_parser(name, help=help_text, description=help_text)
        if name == "begin":
            step.add_argument("channels", nargs="+", metavar="NAME=ID")
            full_help = "Read every channel from the start, and every listed canvas."
            step.add_argument("--full", action="store_true", help=full_help)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "discover":
        return run_slack_step(args)
    if args.command == "collect":
        return run_google_docs_step(args)

    _, requirement_ids = COMMANDS[args.command]
    print(
        f"diffdata {args.command}: not built yet ({requirement_ids}). See docs/requirements.md.",
        file=sys.stderr,
    )
    return 1


def run_slack_step(args: argparse.Namespace) -> int:
    if args.step == "check":
        return slack_inbox.check_from_hook(sys.stdin.buffer.read())
    if args.step == "save":
        return slack_inbox.save_from_hook(sys.stdin.buffer.read())
    try:
        config = load_config()
        if args.step == "channels":
            if not config.slack_channels:
                raise ConfigError(f"No channels are listed under slack_channels in {config.path}.")
            report = "\n".join([f"Opted-in channels, from {config.path}:", *config.slack_channels])
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
        report = google_signin.sign_in()
    except google_signin.SignInError as e:
        print(f"diffdata: {e}", file=sys.stderr)
        return 1
    print(report)
    return 0
