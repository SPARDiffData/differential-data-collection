"""Slack canvases (COL-2): the hook's canvas checks, and building versions and the clean copy
from saved reads. Synthetic fixtures only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata.collect.slack_canvas import show_names, title
from diffdata.common.config import Config
from diffdata.discover.slack_export import build
from diffdata.discover.slack_inbox import begin, open_run, run_path, save

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
    assert "1 channel(s) and 1 canvas(es)" in report
    assert report.endswith(f"Canvases to read: {CANVAS}")


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
    assert saved["saved_at"] == "2026-10-04T12:01:00Z"
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


def test_the_title_is_the_first_heading_else_the_id():
    assert title(json.loads(canvas_read())["markdown_content"], CANVAS) == "Example Project Plan"
    assert title("Just text, no heading.\n", CANVAS) == CANVAS


def test_names_replace_only_mentions():
    people, channels = {"U00000001": "Alex Example"}, {"C00000001": "example-channel"}
    text = "<@U00000001> <#C00000001> <#C00000002|named> <@U00000009> <br> <b>x</b>"
    assert show_names(text, people, channels) == (
        "@Alex Example #example-channel #named @U00000009 <br> <b>x</b>"
    )
