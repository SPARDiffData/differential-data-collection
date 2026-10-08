"""The `diffdata` command.

Every subcommand is a stub for now. Each one names the requirement IDs
(docs/requirements.md) that the first feature Issues will build.
"""

import argparse
import sys

from diffdata import __version__

# subcommand -> (help text, requirement IDs it will implement)
COMMANDS = {
    "discover": ("Find Slack messages and the sources they reference.", "DIS-1, DIS-2, DIS-3"),
    "collect": ("Fetch each referenced source we can access.", "COL-1, COL-2, COL-4, COL-7"),
    "scrub": ("Redact secrets and personal information.", "SCR-1, SCR-2"),
    "push": ("Add scrubbed artifacts to central storage.", "STO-1, STO-2, STO-3"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffdata",
        description="Collect research working data and ready it for storage.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")
    for name, (help_text, _) in COMMANDS.items():
        subcommands.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    _, requirement_ids = COMMANDS[args.command]
    print(
        f"diffdata {args.command}: not built yet ({requirement_ids}). See docs/requirements.md.",
        file=sys.stderr,
    )
    return 1
