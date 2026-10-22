"""Where each channel's next export starts (DIS-1, COL-8).

An incremental run reads a channel only from its start point, and finds new replies in older
threads with a message search. A channel's start point moves forward only after a complete
run, so a skipped page or thread can't leave a silent gap. A run is complete for a channel
when all three hold:
  1. Its channel read reached the last page, starting no later than the planned start. A
     first export, with no start point yet, must read from the beginning.
  2. If the channel read had a start, a message search from no later than the planned start
     also reached its last page.
  3. Every thread the reads listed was read to its last page: the posts with replies from
     the channel read, and the threads the search found new replies in.
A page counts only if it's a result in the connector's shape, every message of which
diffdata could read, with page information that says "last page" or gives the next cursor.
A thread counts only if its read shows the thread's parent post. A read that came back as an
error is saved like any other, but can't make a run complete.
The channel's start point is then that run's start time, and the next run starts OVERLAP
before it. The overlap covers clock differences and the search index's delay. Otherwise the
start point stays, and the next run covers the same stretch again; dedupe drops the repeats.

`build` works the start points out from the whole inbox, run by run from oldest to newest,
the same way it rebuilds the store. slack/state.json only remembers the result for `begin`:
delete it and the next run reads every channel from the beginning, and the next build
writes it again. A run's start time is its run ID, so nothing else is needed.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from diffdata.discover.slack_parse import next_cursor, pages_known, parse_result, result_json

OVERLAP = timedelta(hours=1)
CHANNEL_READ = "slack_read_channel"
THREAD_READ = "slack_read_thread"
SEARCH = "slack_search_public_and_private"
RUN_ID = "%Y%m%dT%H%M%SZ"
UNIX_TIME = re.compile(r"[0-9]{1,10}(?:\.[0-9]+)?")
# A read that sets these leaves messages out with no next cursor to say so: `latest` stops
# before the newest, and a thread is always read in full.
CUT_SHORT = {CHANNEL_READ: {"latest"}, THREAD_READ: {"latest", "oldest"}}
# Why a run didn't move a channel's start point, in the order the build's report lists them.
PAGES = "a channel page wasn't read in full"
LATE = "the channel read started later than planned"
NO_SEARCH = "the search for late replies wasn't finished"
THREADS = "a thread wasn't read in full"
NOT_READ = "the channel wasn't read"
REASONS = (PAGES, LATE, NO_SEARCH, THREADS, NOT_READ)


def state_path(data: Path) -> Path:
    return data / "slack" / "state.json"


def read_state(data: Path) -> dict[str, str]:
    """Channel ID to the start time of its newest complete run. Nothing on record means
    read from the beginning, which is the safe way to fail."""
    try:
        found = json.loads(state_path(data).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, str) and start_time(v) is not None}


def write_state(data: Path, points: dict[str, str]) -> None:
    path = state_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(points, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def start_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def planned_start(point: str | None) -> int | None:
    """Where the next run should start, as Unix seconds: OVERLAP before the start time of
    the newest complete run. None means from the beginning."""
    when = start_time(point) if point else None
    return int((when - OVERLAP).timestamp()) if when else None


def start_points(inbox: Path) -> tuple[dict[str, str], dict[tuple[str, str], str | None]]:
    """Each channel's start point from the whole inbox, and for each (channel, run) why that
    run didn't move it (None: it did)."""
    points: dict[str, str] = {}
    outcomes: dict[tuple[str, str], str | None] = {}
    for channel_dir in sorted(p for p in inbox.glob("*") if p.is_dir()):
        channel = channel_dir.name
        for run_dir in sorted(p for p in channel_dir.glob("*") if p.is_dir()):
            try:
                began = datetime.strptime(run_dir.name, RUN_ID).replace(tzinfo=UTC)
            except ValueError:
                continue
            why = why_incomplete(load(run_dir), planned_start(points.get(channel)))
            outcomes[(channel, run_dir.name)] = why
            if why is None:
                points[channel] = began.strftime("%Y-%m-%dT%H:%M:%SZ")
    return points, outcomes


def load(run_dir: Path) -> list[dict]:
    """One run's saved results for one channel, in the order saved. A file that can't be
    read is left out, so it can only make the run count as incomplete."""
    records = []
    for path in run_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append((str(record.get("saved_at", "")), path.name, record))
    return [record for *_, record in sorted(records, key=lambda r: r[:2])]


def tool(record: dict) -> str:
    return str(record.get("tool_name", "")).rsplit("__", 1)[-1]


def inputs(record: dict) -> dict:
    found = record.get("tool_input")
    return found if isinstance(found, dict) else {}


def read_cursors(record: dict) -> tuple[str | None, str | None]:
    return inputs(record).get("cursor") or None, next_cursor(record.get("tool_response"))


def search_cursors(record: dict) -> tuple[str | None, str | None]:
    return record.get("cursor") or None, record.get("next_cursor") or None


def all_pages(records: list[dict], cursors: Callable) -> list[dict] | None:
    """The pages from a first page (no cursor) to a last page (no next cursor), following
    each next cursor. None if a page in between wasn't saved."""
    by_cursor: dict[str | None, tuple[str | None, dict]] = {}
    for record in records:
        given, following = cursors(record)
        by_cursor.setdefault(given, (following, record))
    pages: list[dict] = []
    cursor = None
    while cursor in by_cursor and len(pages) < len(records):
        following, record = by_cursor[cursor]
        pages.append(record)
        if following is None:
            return pages
        cursor = following
    return None


def seconds(value) -> float | None:
    """A Unix time as Slack takes it, like "1790860440"; None if it isn't one, such as "nan"."""
    return float(value) if isinstance(value, str) and UNIX_TIME.fullmatch(value) else None


def on_time(start, planned: int | None) -> bool:
    """Whether a page that starts at `start` (None: from the beginning) starts no later than
    planned. With nothing planned, only from the beginning will do."""
    if start is None:
        return True
    when = seconds(start)
    return planned is not None and when is not None and when <= planned


def a_read(record: dict) -> bool:
    """Whether a saved channel or thread read is a whole result in the connector's shape:
    JSON with its messages text, every message of which diffdata could read, and page
    information that says either "last page" or where the next one is, from a read that
    wasn't cut short. An error, a message we couldn't read (whose replies we'd never know
    about), or page information we don't know can't count as a page read."""
    found = result_json(record.get("tool_response"))
    return (
        found is not None
        and isinstance(found.get("messages"), str)
        and pages_known(record.get("tool_response"))
        and parse(record)[1] == 0
        and not CUT_SHORT.get(tool(record), set()) & inputs(record).keys()
    )


def parse(record: dict) -> tuple[list, int]:
    return parse_result(
        str(record.get("tool_name", "")), inputs(record), record.get("tool_response")
    )


def messages(record: dict) -> list:
    return parse(record)[0]


def why_incomplete(records: list[dict], planned: int | None) -> str | None:
    """Why one run's saved results for a channel don't make a complete run, or None if they
    do. planned: where the run should have started, or None for from the beginning."""
    reads = [r for r in records if tool(r) == CHANNEL_READ]
    pages = all_pages([r for r in reads if a_read(r)], read_cursors)
    if pages is None:
        return PAGES if reads else NOT_READ
    listed = {m.ts for page in pages for m in messages(page) if m.replies}
    # Every page's start, not just the first: a later page could start later and skip some.
    if not all(on_time(inputs(page).get("oldest"), planned) for page in pages):
        return LATE
    if "oldest" in inputs(pages[0]):
        searches = [r for r in records if tool(r) == SEARCH]
        found = all_pages(searches, search_cursors)
        if found is None or not all(on_time(page.get("after"), planned) for page in found):
            return NO_SEARCH
        listed |= {ts for page in found for ts in page.get("threads", []) if isinstance(ts, str)}
    threads = [r for r in records if tool(r) == THREAD_READ and a_read(r)]
    for ts in listed:
        thread = all_pages([r for r in threads if inputs(r).get("message_ts") == ts], read_cursors)
        # A thread read always shows its parent post, so one that doesn't failed somehow.
        if thread is None or not any(m.ts == ts for page in thread for m in messages(page)):
            return THREADS
    return None
