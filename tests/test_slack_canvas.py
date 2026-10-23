"""Slack canvases (COL-2): the hook's canvas checks, building versions and the clean copy
from saved reads, and the change check that says which canvases to read (COL-8). Synthetic
fixtures only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.collect import slack_canvas
from diffdata.collect.slack_canvas import show_names, title
from diffdata.common.config import Config
from diffdata.discover.slack_export import build, canvases_due
from diffdata.discover.slack_inbox import SlackExportError, begin, open_run, run_path, save

CANVAS = "F00000003"
CHANNEL = "C00000001"
PARENT = "1790864040.000100"
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "slack"
CLEAN_COPY = "slack-canvas_example-project-plan_F00000003.md"


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
        slack_canvases=(CANVAS,),
    )


def canvas_read(fixture: str = "read_canvas_v1.json") -> str:
    return (FIXTURES / fixture).read_text(encoding="utf-8")


def canvas_payload(slack_hook, text=None, canvas=CANVAS, call="toolu_canvas01") -> bytes:
    """A canvas read as the hook gets it: a text block holding the read's JSON."""
    response = [{"type": "text", "text": canvas_read() if text is None else text}]
    return slack_hook("slack_read_canvas", {"canvas_id": canvas}, response, call)


def canvas_dir(config) -> Path:
    return config.data_dir / "documents" / "slack-canvas" / CANVAS


def documents(config) -> list[Path]:
    return sorted(p for p in (config.data_dir / "documents").rglob("*") if p.is_file())


def hook_note(capsys) -> str:
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def export_run(config, slack_response, slack_hook):
    """One /slack-export run: the channel and its thread, so the store knows people's
    names, then the canvas, then the build."""

    def run(when=T0, fixture="read_canvas_v1.json"):
        begin(config, ["example-channel=C00000001"], now=when)
        call = f"toolu_{when:%d%H%M}"
        reads = [
            slack_hook(
                "slack_read_channel",
                {"channel_id": CHANNEL},
                slack_response("read_channel.txt"),
                f"{call}1",
            ),
            slack_hook(
                "slack_read_thread",
                {"channel_id": CHANNEL, "message_ts": PARENT},
                slack_response("read_thread.txt"),
                f"{call}2",
            ),
            canvas_payload(slack_hook, canvas_read(fixture), call=f"{call}3"),
        ]
        for i, payload in enumerate(reads, start=1):
            assert save(config, payload, now=when + timedelta(minutes=i)) == 0
        return build(config)

    return run


# The hook


def test_begin_names_the_opted_in_canvases(config):
    report = begin(config, ["example-channel=C00000001"], now=T0)
    assert open_run(config.data_dir).canvases == [CANVAS]
    assert open_run(config.data_dir).due is None
    assert "1 channel(s) and 1 canvas(es)" in report
    assert CANVAS not in report  # canvases-due says which ones to read


def test_a_run_file_from_before_canvases_still_loads(config):
    begin(config, ["example-channel=C00000001"], now=T0)
    run = json.loads(run_path(config.data_dir).read_text(encoding="utf-8"))
    del run["canvases"]
    run_path(config.data_dir).write_text(json.dumps(run), encoding="utf-8")
    assert open_run(config.data_dir).canvases == []


def test_an_opted_in_canvas_read_is_saved_unchanged(config, slack_hook, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, canvas_payload(slack_hook), now=T0 + timedelta(minutes=1)) == 0
    run_id = open_run(config.data_dir).run_id
    [path] = documents(config)
    assert path == canvas_dir(config) / "raw" / run_id / "slack_read_canvas-toolu_canvas01.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tool_response"] == [{"type": "text", "text": canvas_read()}]
    assert saved["saved_at"] == "2026-10-04T12:01:00.000000Z"  # to the microsecond
    assert "session_id" not in saved and "transcript_path" not in saved
    note = hook_note(capsys)
    assert note.startswith("diffdata saved this result: one canvas, ")
    assert "Example Project Plan" not in note  # counts only


def test_a_canvas_that_isnt_opted_in_is_refused(config, slack_hook, capsys):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, canvas_payload(slack_hook, canvas="F00000002"), now=T0) == 2
    assert "CON-1" in capsys.readouterr().err
    assert documents(config) == []


@pytest.mark.parametrize(
    "text",
    [
        canvas_read().replace(CANVAS, "F00000002"),  # shows another canvas
        json.dumps({"markdown_content": "# No ID"}),  # shows no canvas at all
        json.dumps({"canvas_id": CANVAS}),  # has no text
        "not JSON",
    ],
)
def test_a_read_that_doesnt_show_the_canvas_asked_for_is_refused(config, slack_hook, capsys, text):
    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, canvas_payload(slack_hook, text), now=T0) == 2
    assert "Stop the export" in capsys.readouterr().err
    assert documents(config) == []


def test_nothing_is_saved_without_an_open_run(config, slack_hook):
    assert save(config, canvas_payload(slack_hook), now=T0) == 0
    assert documents(config) == []


def test_a_canvas_too_big_for_claude_is_saved_from_claude_codes_copy(
    config, slack_hook, capsys, tmp_path
):
    # Claude Code's layout: the transcript, and beside it the session's tool-results folder.
    transcript = tmp_path / "claude" / "projects" / "repo" / "session-0001.jsonl"
    folder = transcript.with_suffix("") / "tool-results"
    folder.mkdir(parents=True)
    copy = folder / "mcp-claude_ai_Slack-slack_read_canvas-0001.txt"
    copy.write_text(canvas_read(), encoding="utf-8")
    notice = (
        "Error: result (90,000 characters across 1 line) exceeds maximum allowed tokens. "
        f"Output has been saved to {copy}.\nFormat: Plain text"
    )
    hook = json.loads(canvas_payload(slack_hook, notice))
    hook["transcript_path"] = str(transcript)

    begin(config, ["example-channel=C00000001"], now=T0)
    assert save(config, json.dumps(hook).encode("utf-8"), now=T0) == 0
    [path] = documents(config)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tool_response"] == canvas_read()
    assert saved["overflow_file"] == copy.name
    assert "Don't read that file" in hook_note(capsys)


# The build


def version_files(config) -> list[Path]:
    return sorted((canvas_dir(config) / "versions").glob("*.md"))


def clean_copy(config) -> str:
    return (config.data_dir / "documents" / "current" / CLEAN_COPY).read_text(encoding="utf-8")


def test_the_first_read_gives_one_version_and_a_clean_copy(config, export_run):
    report = export_run()
    assert "1 canvas(es): 1 read(s), 1 version(s)" in report
    [version] = version_files(config)
    assert version.read_text(encoding="utf-8") == json.loads(canvas_read())["markdown_content"]
    copy = clean_copy(config)
    assert copy.startswith('---\ntitle: "Example Project Plan"\nsource: slack-canvas\n')
    meta = json.loads((canvas_dir(config) / "meta.json").read_text(encoding="utf-8"))
    assert meta["workspace"] == "https://example.slack.com"
    assert meta["last_collected"] == "2026-10-04T12:03:00Z"


def test_building_again_adds_nothing(config, export_run):
    export_run()
    assert "0 file(s) written" in build(config)


def test_the_same_text_read_again_adds_no_version(config, export_run):
    export_run()
    export_run(T0 + timedelta(hours=1))
    assert len(version_files(config)) == 1


def test_a_changed_canvas_adds_a_version_and_updates_the_clean_copy(config, export_run):
    export_run()
    export_run(T0 + timedelta(hours=1))
    report = export_run(T0 + timedelta(days=1), fixture="read_canvas_v2.json")
    assert "1 canvas(es): 3 read(s), 2 version(s)" in report
    assert len(version_files(config)) == 2
    events = (canvas_dir(config) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    second = json.loads(events[-1])
    assert second["type"] == "version" and second["origin"] == "snapshot"
    # The edit happened after the last read that still showed the old text.
    assert second["not_before"] == "2026-10-04T13:03:00Z"
    assert second["seen_at"] == "2026-10-05T12:03:00Z"
    assert "## Decisions" in clean_copy(config)
    assert "read-only<br>for now" not in clean_copy(config)


def test_the_clean_copy_shows_names_and_leaves_the_rest_as_written(config, export_run):
    export_run()
    copy = clean_copy(config)
    assert "Owner: @Alex Example. Questions go in #example-channel." in copy
    assert "Ask @U00000009 about the timing :question:" in copy  # unknown: stays an ID
    assert "read-only<br>for now" in copy
    assert "[Plan doc](https://docs.google.com/document/d/FAKE-DOC-0001/edit)" in copy
    assert "![diagram.png](https://example.slack.com/files/U00000001/F00000009/diagram.png)" in copy
    # The version keeps the text as fetched, so a renamed person doesn't make a new version.
    assert "<@U00000001>" in version_files(config)[0].read_text(encoding="utf-8")


def test_comment_threads_and_section_ids_stay_in_raw_only(config, export_run):
    export_run()
    derived = [p for p in documents(config) if "raw" not in p.relative_to(config.data_dir).parts]
    assert derived
    for path in derived:
        text = path.read_text(encoding="utf-8")
        assert "1790950000.000100" not in text and "temp:C:" not in text
    assert not (canvas_dir(config) / "comments.md").exists()


def test_the_report_gives_counts_only(export_run):
    report = export_run()
    for private in ("Example Project Plan", "Alex", "FAKE-DOC-0001", CANVAS):
        assert private not in report


@pytest.mark.parametrize("read", [0, 1, 2])
def test_the_report_counts_the_listed_canvases_this_run_saved(
    config, slack_response, slack_hook, read
):
    # A test run reads only the first canvas, and a hook that missed the big ones saves fewer.
    listed = (CANVAS, "F00000004")
    config = replace(config, slack_canvases=listed)
    begin(config, ["example-channel=C00000001"], now=T0)
    channel = slack_hook(
        "slack_read_channel", {"channel_id": CHANNEL}, slack_response("read_channel.txt")
    )
    assert save(config, channel, now=T0) == 0
    for i, canvas in enumerate(listed[:read]):
        payload = canvas_payload(
            slack_hook, canvas_read().replace(CANVAS, canvas), canvas=canvas, call=f"toolu_c{i}"
        )
        assert save(config, payload, now=T0) == 0
    assert f"This run saved reads for {read} of 2 listed canvas(es)." in build(config)


def test_a_canvas_with_an_unreadable_read_is_left_as_it_was(config, export_run):
    export_run()
    export_run(T0 + timedelta(days=1), fixture="read_canvas_v2.json")
    before = {p: p.read_bytes() for p in documents(config)}
    broken = canvas_dir(config) / "raw" / "20261005T120000Z" / "slack_read_canvas-broken.json"
    broken.write_text("not JSON", encoding="utf-8")
    report = build(config)
    assert "Couldn't read 1 saved canvas read(s), so 1 canvas(es) were left as they were" in report
    # Both versions and the newer clean copy are still there, unchanged.
    assert {p: p.read_bytes() for p in documents(config) if p != broken} == before


@pytest.mark.parametrize("newer_first", [False, True])
def test_two_reads_in_the_same_second_keep_their_order(config, slack_hook, newer_first):
    # Saved times keep the microseconds, so the newer read wins whatever order they arrive in.
    reads = [
        (T0 + timedelta(microseconds=100_000), "# Older\n\nold text\n"),
        (T0 + timedelta(microseconds=900_000), "# Newer\n\nnew text\n"),
    ]
    begin(config, ["example-channel=C00000001"], now=T0)
    for i, (when, text) in enumerate(reversed(reads) if newer_first else reads):
        read = json.dumps({"canvas_id": CANVAS, "markdown_content": text})
        assert save(config, canvas_payload(slack_hook, read, call=f"toolu_s{i}"), now=when) == 0
    build(config)
    [current] = (config.data_dir / "documents" / "current").glob("*.md")
    assert current.name == "slack-canvas_newer_F00000003.md"
    assert current.read_text(encoding="utf-8").endswith("# Newer\n\nnew text\n")


def test_the_title_is_the_first_heading_else_the_id():
    assert title(json.loads(canvas_read())["markdown_content"], CANVAS) == "Example Project Plan"
    assert title("Just text, no heading.\n", CANVAS) == CANVAS


def test_names_replace_only_mentions():
    people, channels = {"U00000001": "Alex Example"}, {"C00000001": "example-channel"}
    text = "<@U00000001> <#C00000001> <#C00000002|named> <@U00000009> <br> <b>x</b>"
    assert show_names(text, people, channels) == (
        "@Alex Example #example-channel #named @U00000009 <br> <b>x</b>"
    )


# The change check (COL-8): a files search's sizes, then canvases-due

SEARCH_TOOL = "slack_search_public_and_private"
SEARCH_INPUT = {
    "content_types": "files",
    "filters": f"in:<#{CHANNEL}>",
    "channel_types": "public_channel,private_channel",
    "natural_language_query": "",
    "response_format": "detailed",
    "include_context": False,
    "limit": 20,
}
LISTED = (CANVAS, "F00000004")
UNLISTED = ("F00000008", "Unlisted Planning Example")  # a canvas in the search, not listed


def fixture_results() -> str:
    return json.loads((FIXTURES / "search_files.json").read_text(encoding="utf-8"))["results"]


def search_fixture(results=None) -> list[dict]:
    """The hand-written files search, in the connector's wrapping, optionally with other
    results text."""
    inner = json.loads((FIXTURES / "search_files.json").read_text(encoding="utf-8"))
    if results is not None:
        inner["results"] = results
    return [{"type": "text", "text": json.dumps(inner)}]


def files_search(sizes: dict[str, str]) -> list[dict]:
    """A files search showing these canvases and sizes, in the fixture's shape."""
    rows = "".join(
        f"### Result {i} of {len(sizes)}\nTitle: Canvas {i} \nFile type: canvas\n"
        f"Size: {size}\nAuthor: Alex Example <alex@example.com> (U00000001)\n"
        f"Created: 2026-09-01 09:00:00 CDT\nFile ID: {canvas}\n"
        f"Permalink: [link](https://example.slack.com/docs/T00000001/{canvas})\n\n---\n\n"
        "Content: \n\n\n\n---\n\n"
        for i, (canvas, size) in enumerate(sizes.items(), start=1)
    )
    inner = {
        "results": f"# Search Results for: \n\n## Files ({len(sizes)} results)\n{rows}",
        "pagination_info": "End of results - No more pages available.\n",
    }
    return [{"type": "text", "text": json.dumps(inner)}]


def read_the_channel(config, slack_hook, slack_response, when=T0) -> None:
    """A channel read, which the files search needs first."""
    channel = slack_hook(
        "slack_read_channel",
        {"channel_id": CHANNEL},
        slack_response("read_channel.txt"),
        f"toolu_ch{when:%d%H%M}",
    )
    assert save(config, channel, now=when) == 0


def size_files(config) -> list[Path]:
    return sorted((config.data_dir / "slack" / "canvas-sizes").rglob("*.json"))


def test_a_files_search_keeps_only_the_listed_canvases_sizes(
    config, slack_hook, slack_response, capsys
):
    config = replace(config, slack_canvases=LISTED)
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    capsys.readouterr()
    search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, search_fixture(), "toolu_search01")
    assert save(config, search, now=T0 + timedelta(minutes=1)) == 0
    run_id = open_run(config.data_dir).run_id
    [path] = size_files(config)
    assert path.relative_to(config.data_dir).as_posix() == (
        f"slack/canvas-sizes/{run_id}/search_files-toolu_search01.json"
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "saved_at": "2026-10-04T12:01:00Z",
        "run_id": run_id,
        "channel_id": CHANNEL,
        "sizes": {CANVAS: "12.3 KB", "F00000004": "4.5 KB"},
    }
    assert hook_note(capsys) == (
        "diffdata saved this result: 4 file(s), 2 of them listed canvases. "
        "Next page: call again with cursor `RkFLRUZJTEVTMDE=`."
    )
    # CON-1: a file that isn't listed never reaches disk, in any file.
    for saved in config.data_dir.rglob("*"):
        if saved.is_file():
            text = saved.read_text(encoding="utf-8")
            assert not any(private in text for private in UNLISTED)


@pytest.mark.parametrize(
    "broken",
    [
        fixture_results().replace("Size: 4.5 KB\n", ""),  # a listed canvas with no size
        fixture_results().replace("File ID: F00000009\n", ""),  # a file with no ID
        fixture_results().replace("File ID: F00000009", "File ID: example"),  # not an ID
        fixture_results().replace("(4 results)", "(5 results)"),  # a file diffdata didn't find
        fixture_results().replace("## Files (4 results)\n", ""),  # files, but no count
        fixture_results().replace("Size: 4.5 KB", "Size: about four"),  # not a size
        fixture_results().replace("Size: 4.5 KB", "Size: 7 Alice"),  # not a byte unit
        fixture_results().replace("Size: 4.5 KB", "Size: 4.5 KB\nSize: 9.9 KB"),  # two sizes
        # A title or content with line breaks mustn't pass off its own text as a listed
        # canvas's ID or size, even with a Content: line of its own.
        fixture_results().replace(
            "Title: diagram.png \n",
            "Title: diagram.png\nFile ID: F00000003\nSize: riley@example.com \n",
        ),
        fixture_results().replace(
            "Title: diagram.png \n",
            "Title: diagram.png\nFile ID: F00000003\nSize: 7 KB\nContent: \n",
        ),
        fixture_results().replace(
            "Title: diagram.png \n",
            "Title: diagram.png\n### Result 5 of 5\nFile ID: F00000003\nSize: 1.0 KB \n",
        ),
        fixture_results().replace(
            "Content: \ndiagram.png", "Content: \nSize: 99.9 KB\nFile ID: F00000003"
        ),
        # The same file shown twice, with two sizes.
        fixture_results().replace("File ID: F00000009", "File ID: F00000003"),
    ],
)
def test_a_files_search_diffdata_cant_read_is_refused(
    config, slack_hook, slack_response, capsys, broken
):
    config = replace(config, slack_canvases=LISTED)
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, search_fixture(broken))
    assert save(config, search, now=T0) == 2
    assert "Stop the export" in capsys.readouterr().err
    assert size_files(config) == []


@pytest.mark.parametrize(
    "results",
    [
        # A file that isn't a canvas, with no size, one in another shape, or two.
        fixture_results().replace("Size: 51.7 KB\n", ""),
        fixture_results().replace("Size: 51.7 KB", "Size: unknown"),
        fixture_results().replace("Size: 51.7 KB", "Size: 51.7 KB\nSize: 9.9 KB"),
        # A canvas that isn't listed, with two sizes.
        fixture_results().replace("Size: 2.0 KB", "Size: 2.0 KB\nSize: 9.9 KB"),
    ],
)
def test_a_file_that_isnt_a_listed_canvas_needs_no_size(
    config, slack_hook, slack_response, capsys, results
):
    # Only listed canvases' sizes are kept, so another file can't stop the export (#58).
    config = replace(config, slack_canvases=LISTED)
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    capsys.readouterr()
    assert save(config, slack_hook(SEARCH_TOOL, SEARCH_INPUT, search_fixture(results)), now=T0) == 0
    [path] = size_files(config)
    assert json.loads(path.read_text(encoding="utf-8"))["sizes"] == {
        CANVAS: "12.3 KB",
        "F00000004": "4.5 KB",
    }
    assert "4 file(s), 2 of them listed canvases" in hook_note(capsys)


@pytest.mark.parametrize("size", ["0 Bytes", "0 bytes", "512 B", "26.5 KB", "244.4 MB", "1 GB"])
def test_a_listed_canvas_size_in_any_byte_unit_is_kept(config, slack_hook, slack_response, size):
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, files_search({CANVAS: size}))
    assert save(config, search, now=T0) == 0
    [path] = size_files(config)
    assert json.loads(path.read_text(encoding="utf-8"))["sizes"] == {CANVAS: size}


@pytest.mark.parametrize("response", ["not JSON", [{"type": "text", "text": '{"other": 1}'}]])
def test_a_files_search_without_results_is_refused(
    config, slack_hook, slack_response, capsys, response
):
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    assert save(config, slack_hook(SEARCH_TOOL, SEARCH_INPUT, response), now=T0) == 2
    assert "Stop the export" in capsys.readouterr().err
    assert size_files(config) == []


def test_the_build_doesnt_read_canvas_sizes_as_messages(config, slack_hook, slack_response):
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    assert save(config, slack_hook(SEARCH_TOOL, SEARCH_INPUT, search_fixture()), now=T0) == 0
    assert "1 channel(s): 1 result file(s)" in build(config)


LAST = T0 + timedelta(minutes=3)


@pytest.mark.parametrize(
    "last, size_then, size_now, notice, expected",
    [
        (None, None, "1.0 KB", None, [slack_canvas.NEVER_READ]),
        (LAST, "1.0 KB", "1.0 KB", None, []),
        (LAST, "1.0 KB", "1.1 KB", None, [slack_canvas.SIZE_CHANGED]),
        (LAST, None, "1.0 KB", None, [slack_canvas.NO_OLD_SIZE]),
        (LAST, "1.0 KB", None, None, [slack_canvas.MISSING]),
        (LAST, "1.0 KB", "1.0 KB", LAST + timedelta(hours=1), [slack_canvas.NOTICE]),
        (LAST, "1.0 KB", "1.0 KB", LAST - timedelta(hours=1), []),
    ],
)
def test_the_reasons_to_read_a_canvas(last, size_then, size_now, notice, expected):
    now = T0 + timedelta(days=1)
    assert slack_canvas.reasons(last, size_then, size_now, notice, 7, now) == expected


def test_a_canvas_is_read_again_after_refresh_days():
    week = LAST + timedelta(days=7)
    same = (LAST, "1.0 KB", "1.0 KB", None, 7)
    assert slack_canvas.reasons(*same, week) == [slack_canvas.REFRESH]
    assert slack_canvas.reasons(*same, week - timedelta(seconds=1)) == []


def test_tab_notices_come_from_slackbot_only_and_the_newest_wins():
    notice = "Alex Example made updates to a canvas tab: F00000003"
    messages = [
        {"user_id": "USLACKBOT", "text": notice, "ts": "1790955177.000300"},
        {"user_id": "USLACKBOT", "text": notice, "ts": "1790900000.000100"},  # older
        {"user_id": "U00000001", "text": notice.replace("03", "04"), "ts": "1790999999.000100"},
        {"user_id": "USLACKBOT", "text": "Reminder: stand-up", "ts": "1790999999.000200"},
    ]
    assert slack_canvas.tab_notices(messages) == {
        "F00000003": datetime.fromtimestamp(1790955177, UTC)
    }


@pytest.fixture
def due_run(config, slack_response, slack_hook):
    """One /slack-export run with the change check: the channel, a files search showing
    `sizes` (None: no search), canvases-due, a read of each canvas it lists (or of `read`),
    then the build. Returns canvases-due's report and the build's."""

    def run(when, sizes, read=None, config=config):
        begin(config, ["example-channel=C00000001"], now=when)
        read_the_channel(config, slack_hook, slack_response, when)
        if sizes is not None:
            search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, files_search(sizes), "toolu_search")
            assert save(config, search, now=when + timedelta(minutes=1)) == 0
        due = canvases_due(config, now=when + timedelta(minutes=2))
        canvases = open_run(config.data_dir).due if read is None else read
        for i, canvas in enumerate(canvases):
            text = canvas_read().replace(CANVAS, canvas)
            payload = canvas_payload(slack_hook, text, canvas=canvas, call=f"toolu_r{i}")
            assert save(config, payload, now=when + timedelta(minutes=3)) == 0
        return due, build(config)

    return run


def test_an_unchanged_canvas_is_skipped(due_run):
    due, built = due_run(T0, {CANVAS: "12.3 KB"})
    assert due == (
        "1 of 1 listed canvas(es) to read.\n"
        "Why (a canvas can have more than one reason): 1 never read.\n"
        f"Canvases to read: {CANVAS}"
    )
    assert "This run saved reads for 1 of 1 due canvas(es), 1 listed." in built
    due, built = due_run(T0 + timedelta(hours=1), {CANVAS: "12.3 KB"})
    assert due == "0 of 1 listed canvas(es) to read.\nNo canvases to read."
    assert "This run saved reads for 0 of 0 due canvas(es), 1 listed." in built


def test_canvases_due_keeps_its_list_in_the_run(config, due_run):
    due_run(T0, {CANVAS: "12.3 KB"})
    begin(config, ["example-channel=C00000001"], now=T0 + timedelta(hours=1))
    canvases_due(config, now=T0 + timedelta(hours=1))  # no search this time
    assert open_run(config.data_dir).due == [CANVAS]


def test_a_canvas_whose_size_changed_is_read(due_run):
    due_run(T0, {CANVAS: "12.3 KB"})
    due, _ = due_run(T0 + timedelta(hours=1), {CANVAS: "12.4 KB"})
    assert "1 size changed." in due and f"Canvases to read: {CANVAS}" in due


def test_a_canvas_whose_read_failed_is_tried_again(due_run):
    # The size is compared with the size at the last read, not the last size seen.
    due_run(T0, {CANVAS: "12.3 KB"})
    due_run(T0 + timedelta(hours=1), {CANVAS: "12.4 KB"}, read=())  # the read didn't happen
    due, _ = due_run(T0 + timedelta(hours=2), {CANVAS: "12.4 KB"})
    assert "1 size changed." in due and f"Canvases to read: {CANVAS}" in due


def test_a_canvas_shown_with_two_sizes_in_one_run_is_read(
    config, due_run, slack_hook, slack_response
):
    # Two pages, or two channels' searches, can disagree while a canvas is being edited.
    # Whichever saved file sorts last, the canvas is read.
    due_run(T0, {CANVAS: "12.3 KB"})
    when = T0 + timedelta(hours=1)
    begin(config, ["example-channel=C00000001"], now=when)
    read_the_channel(config, slack_hook, slack_response, when)
    for call, size in (("toolu_a", "12.4 KB"), ("toolu_b", "12.3 KB")):  # the old size last
        search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, files_search({CANVAS: size}), call)
        assert save(config, search, now=when) == 0
    assert "1 size changed." in canvases_due(config, now=when)


def test_a_files_search_too_big_for_claude_is_filtered_from_claude_codes_copy(
    config, slack_hook, slack_response, capsys, tmp_path
):
    # Claude Code's layout: the transcript, and beside it the session's tool-results folder.
    transcript = tmp_path / "claude" / "projects" / "repo" / "session-0001.jsonl"
    folder = transcript.with_suffix("") / "tool-results"
    folder.mkdir(parents=True)
    copy = folder / "mcp-claude_ai_Slack-slack_search_public_and_private-0001.txt"
    copy.write_text(search_fixture()[0]["text"], encoding="utf-8")
    notice = (
        "Error: result (90,000 characters across 1 line) exceeds maximum allowed tokens. "
        f"Output has been saved to {copy}.\nFormat: Plain text"
    )
    hook = json.loads(slack_hook(SEARCH_TOOL, SEARCH_INPUT, [{"type": "text", "text": notice}]))
    hook["transcript_path"] = str(transcript)

    config = replace(config, slack_canvases=LISTED)
    begin(config, ["example-channel=C00000001"], now=T0)
    read_the_channel(config, slack_hook, slack_response)
    capsys.readouterr()
    assert save(config, json.dumps(hook).encode("utf-8"), now=T0) == 0
    [path] = size_files(config)
    assert json.loads(path.read_text(encoding="utf-8"))["sizes"] == {
        CANVAS: "12.3 KB",
        "F00000004": "4.5 KB",
    }
    assert "4 file(s), 2 of them listed canvases" in hook_note(capsys)


@pytest.mark.parametrize("sizes", [{}, {"F00000004": "1.0 KB"}, None])  # None: no search
def test_a_canvas_missing_from_the_search_is_read(due_run, sizes):
    due_run(T0, {CANVAS: "12.3 KB"})
    due, _ = due_run(T0 + timedelta(hours=1), sizes)
    assert "1 not in this run's files search." in due and f"Canvases to read: {CANVAS}" in due


def test_a_canvas_read_without_a_size_on_record_is_read_once_more(due_run):
    # Reads saved before the change check have no size, so the first run after it reads them.
    due_run(T0, None)
    due, _ = due_run(T0 + timedelta(hours=1), {CANVAS: "12.3 KB"})
    assert "1 no size from its last read." in due
    due, _ = due_run(T0 + timedelta(hours=2), {CANVAS: "12.3 KB"})
    assert "No canvases to read." in due


def test_a_canvas_tab_notice_since_the_last_read_means_a_read(config, due_run):
    # read_channel.txt has Slackbot's notice for F00000002 at 2026-10-02 15:32:57 UTC.
    config = replace(config, slack_canvases=("F00000002",))
    before = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    due_run(before, {"F00000002": "1.0 KB"}, config=config)
    due, _ = due_run(before + timedelta(days=2), {"F00000002": "1.0 KB"}, config=config)
    assert "1 canvas-tab notice since its last read." in due
    due, _ = due_run(before + timedelta(days=2, hours=1), {"F00000002": "1.0 KB"}, config=config)
    assert "No canvases to read." in due


def test_a_canvas_not_read_for_refresh_days_is_read(config, due_run):
    config = replace(config, canvas_refresh_days=3)
    due_run(T0, {CANVAS: "12.3 KB"}, config=config)
    due, _ = due_run(T0 + timedelta(days=2), {CANVAS: "12.3 KB"}, config=config)
    assert "No canvases to read." in due
    due, _ = due_run(T0 + timedelta(days=3, hours=1), {CANVAS: "12.3 KB"}, config=config)
    assert "1 last read too long ago." in due


def test_a_full_run_reads_every_listed_canvas(config, due_run, slack_hook, slack_response):
    due_run(T0, {CANVAS: "12.3 KB"})
    when = T0 + timedelta(hours=1)
    begin(config, ["example-channel=C00000001"], now=when, full=True)
    read_the_channel(config, slack_hook, slack_response, when)
    search = slack_hook(SEARCH_TOOL, SEARCH_INPUT, files_search({CANVAS: "12.3 KB"}), "toolu_s2")
    assert save(config, search, now=when + timedelta(minutes=1)) == 0
    due = canvases_due(config, now=when + timedelta(minutes=2))
    assert due == (
        "1 of 1 listed canvas(es) to read.\n"
        "Why (a canvas can have more than one reason): 1 full run.\n"
        f"Canvases to read: {CANVAS}"
    )


def test_canvases_due_names_only_ids_and_counts(due_run):
    due, _ = due_run(T0, {CANVAS: "12.3 KB"})
    for private in ("Example Project Plan", "Canvas 1", "12.3", "Alex"):
        assert private not in due


def test_canvases_due_needs_an_open_run(config):
    with pytest.raises(SlackExportError, match="No export run is open"):
        canvases_due(config, now=T0)
    begin(config, ["example-channel=C00000001"], now=T0)
    with pytest.raises(SlackExportError, match="No export run is open"):
        canvases_due(config, now=T0 + timedelta(minutes=61))  # expired


def test_cli_canvases_due(tmp_path, monkeypatch, capsys):
    data = (tmp_path / "data").as_posix()
    (tmp_path / "config.toml").write_text(
        f'[sources]\nslack_channels = ["example-channel"]\nslack_canvases = ["{CANVAS}"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f'[local]\ndata_dir = "{data}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["discover", "slack", "canvases-due"]) == 1
    assert "No export run is open" in capsys.readouterr().err
    assert cli.main(["discover", "slack", "begin", "example-channel=C00000001"]) == 0
    assert cli.main(["discover", "slack", "canvases-due"]) == 0
    assert capsys.readouterr().out.endswith(f"Canvases to read: {CANVAS}\n")
