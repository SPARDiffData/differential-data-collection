"""Build the Slack export from the inbox (DIS-1): the message store, then the markdown view.

Under the data folder:
  slack/store/<channel-id>/messages.jsonl        append-only, one line per message version
  slack/export/<channel-name>/<YYYY-MM-DD>.md    readable view, one file per UTC day

Every build reads the whole inbox, so the store can be rebuilt after a parser fix. A message
version the store already has is skipped, so building twice adds nothing. If a message's text
or files changed, both versions are kept, and the view shows the newest one.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from diffdata import __version__
from diffdata.common.config import Config
from diffdata.discover.slack_inbox import (
    close_run,
    inbox_dir,
    open_run,
    require_workspace,
    run_channel_names,
    slack_dir,
)
from diffdata.discover.slack_parse import parse_result, permalink, to_markdown


def build(config: Config) -> str:
    """Build the store and the view for every channel in the inbox. Returns counts only."""
    require_workspace(config)
    data = config.data_dir
    names = run_channel_names(data)
    run = open_run(data)
    totals: Counter = Counter()
    channel_dirs = sorted(p for p in inbox_dir(data).glob("*") if p.is_dir())
    for channel_dir in channel_dirs:
        channel_id = channel_dir.name
        records, counts = read_inbox(channel_dir, channel_id, config.slack_workspace_url)
        store_file = slack_dir(data) / "store" / channel_id / "messages.jsonl"
        totals.update(counts)
        totals["new"] += add_to_store(store_file, records)
        name = names.get(channel_id, channel_id)
        folder = slack_dir(data) / "export" / re.sub(r"[^a-z0-9_-]", "-", name.lower())
        totals["days"] += write_view(
            folder, channel_id, name, names, config.slack_workspace_url, read_store(store_file)
        )

    report = [
        f"{len(channel_dirs)} channel(s): {totals['results']} result file(s), "
        f"{totals['messages']} message(s) read, {totals['new']} new or changed, "
        f"{totals['days']} day file(s) written."
    ]
    if totals["skipped"] or totals["unreadable"]:
        report.append(
            f"Couldn't read {totals['skipped']} message(s) and {totals['unreadable']} result "
            "file(s). The connector's format may have changed. Nothing is lost: the raw "
            "results stay in the inbox."
        )
    if totals["empty"]:
        report.append(f"{totals['empty']} result file(s) had no messages in them.")
    if run is None:
        report.append("No export run was open, so this rebuilt from the inbox.")
    else:
        if not any(inbox_dir(data).glob(f"*/{run.run_id}/*.json")):
            report.append(
                "No results were saved during this run. Is the hook running, and is uv on PATH?"
            )
        close_run(data, run, "built")
        report.append(f"Closed export run {run.run_id}.")
    return "\n".join(report)


def read_inbox(channel_dir: Path, channel_id: str, workspace: str) -> tuple[list[dict], Counter]:
    """Store records for every message in a channel's inbox, oldest result first."""
    counts: Counter = Counter()
    saved = []
    for path in channel_dir.glob("*/*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            saved.append((result["saved_at"], path.name, result))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            counts["unreadable"] += 1
    records = []
    for saved_at, _, result in sorted(saved, key=lambda s: s[:2]):
        messages, skipped = parse_result(
            result["tool_name"], result["tool_input"], result["tool_response"]
        )
        counts.update(results=1, messages=len(messages), skipped=skipped, empty=not messages)
        for m in messages:
            records.append(
                {
                    "channel_id": channel_id,
                    "ts": m.ts,
                    "thread_ts": m.thread_ts,
                    "user_id": m.user_id,
                    "user_name": m.user_name,
                    "bot": m.bot,
                    "text": m.text,
                    "files": m.files,
                    "reactions": m.reactions,
                    "permalink": permalink(workspace, channel_id, m.ts, m.thread_ts),
                    "content_hash": m.content_hash(),
                    "collected_at": saved_at,
                    "run_id": result.get("run_id"),
                }
            )
    return records, counts


def read_store(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def store_key(record: dict) -> tuple:
    # thread_ts is part of the key: a reply also sent to the channel shows up in the channel
    # read as a plain post, and the thread read's copy is what says which thread it's in.
    return record["ts"], record["content_hash"], record["thread_ts"] or ""


def add_to_store(path: Path, records: list[dict]) -> int:
    """Append the message versions the store doesn't have yet. Returns how many."""
    seen = {store_key(r) for r in read_store(path)}
    new = []
    for record in records:
        key = store_key(record)
        if key not in seen:
            seen.add(key)
            new.append(record)
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in new)
    return len(new)


def ts_key(record: dict) -> tuple[int, ...]:
    return tuple(int(part) for part in record["ts"].split("."))


def utc(ts: str) -> datetime:
    return datetime.fromtimestamp(int(ts.split(".")[0]), UTC)


def write_view(
    folder: Path,
    channel_id: str,
    name: str,
    channels: dict[str, str],
    workspace: str,
    store: list[dict],
) -> int:
    """Write one markdown file per UTC day, with thread replies under their parent post,
    even when a reply came days later. Returns how many files changed."""
    latest: dict[str, dict] = {}
    thread_of: dict[str, str] = {}
    hashes: dict[str, set] = defaultdict(set)
    for record in store:  # in the order collected, so the newest version wins
        latest[record["ts"]] = record
        hashes[record["ts"]].add(record["content_hash"])
        if record["thread_ts"]:  # any copy that knows its thread places the message
            thread_of[record["ts"]] = record["thread_ts"]
    people = {r["user_id"]: r["user_name"] for r in store if r["user_id"]}

    posts, replies = [], defaultdict(list)
    for record in latest.values():
        parent = thread_of.get(record["ts"])
        if parent and parent != record["ts"] and parent in latest:
            replies[parent].append(record)
        else:
            posts.append(record)
    days = defaultdict(list)
    for post in posts:
        days[utc(post["ts"]).date().isoformat()].append(post)

    def render(record: dict, day: str) -> list[str]:
        when = utc(record["ts"])
        stamp = f"{when:%H:%M}" if when.date().isoformat() == day else f"{when:%Y-%m-%d %H:%M}"
        head = f"**{record['user_name']}**" + (" (bot)" if record["bot"] else "")
        head += f" · {stamp} UTC · [link]({record['permalink']})"
        if len(hashes[record["ts"]]) > 1:
            head += " · edited"
        lines = [head]
        if record["text"]:
            lines += to_markdown(record["text"], people, channels).split("\n")
        if record["files"]:
            listed = [
                f"{f['name']} ({f['id']}, {f['type']})" if f["id"] else f["name"]
                for f in record["files"]
            ]
            lines.append("Files: " + ", ".join(listed))
        return lines

    changed = 0
    for day, day_posts in sorted(days.items()):
        shown, body = [], []
        for post in sorted(day_posts, key=ts_key):
            thread = sorted(replies.get(post["ts"], []), key=ts_key)
            shown += [post, *thread]
            body += ["", *render(post, day)]
            for i, reply in enumerate(thread):
                body += [""] if i == 0 else [">"]
                body += [f"> {line}" if line else ">" for line in render(reply, day)]
        header = [
            "---",
            "source: slack",
            f"workspace: {workspace}",
            f"channel: {name}",
            f"channel_id: {channel_id}",
            f"date: {day}",
            "timezone: UTC",
            f"messages: {len(shown)}",
            f"collected_at: {max(r['collected_at'] for r in shown)}",
            f"tool: diffdata {__version__}",
            "---",
            "",
            f"# #{name}, {day}",
        ]
        text = "\n".join(header + body) + "\n"
        path = folder / f"{day}.md"
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            changed += 1
    return changed
