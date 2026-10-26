"""The reference log (DIS-2, DIS-3, COL-7): every source the saved Slack data points to, once
each, with every place it was referenced. Synthetic fixtures only."""

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import __version__
from diffdata.collect import slack_canvas
from diffdata.common.config import Config
from diffdata.discover.references import in_text, render
from diffdata.discover.slack_export import build

CHANNEL = "C00000001"
PARENT = "1791295200.000100"
REPLY = "1791297000.000110"
CANVAS = "F00000010"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "slack"
DOC = "google:FAKE-DOC-0010"
SHEET = "google:FAKE-SHEET-0010"
SLIDES = "google:FAKE-SLIDES-0010"
PAPER = "url:https://example.org/paper?id=7"
SHEET_LINK = "<https://docs.google.com/spreadsheets/d/FAKE-SHEET-0010/edit#gid=0|Budget sheet>"
PAPER_LINE = "Reading: https://example.org/paper?id=7\\n\\n"  # as the canvas fixture's JSON has it
EVERY_SOURCE = {
    "slack-file:F00000010": "slack-canvas",
    "slack-file:F00000011": "slack-attachment",
    "slack-file:F00000012": "slack-attachment",
    "slack-file:F00000013": "slack-canvas",
    "slack-file:F00000014": "slack-canvas",
    "slack-file:F00000015": "slack-attachment",
    DOC: "google-doc",
    SHEET: "google-sheet",
    SLIDES: "google-slides",
    "google:FAKE-FILE-0010": "google-file",
    PAPER: "link",
    "url:https://code.example.com/example/repo": "link",
    "url:https://drive.google.com/drive/folders/FAKE-FOLDER-0010": "link",
    "url:https://example.slack.com/archives/C00000001/p1791295200000100": "link",
}


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
        slack_canvases=(CANVAS,),
    )


@pytest.fixture
def export(config, slack_run, slack_response):
    """One /slack-export run, then the build: the channel, its thread and the listed canvas.
    The channel read or the canvas read can be an edited copy."""

    def run(when=T0, channel="references_channel.txt", canvas="references_canvas.json"):
        canvas_text = (FIXTURES / canvas).read_text(encoding="utf-8")
        reads = [
            ("slack_read_channel", {"channel_id": CHANNEL}, slack_response(channel)),
            (
                "slack_read_thread",
                {"channel_id": CHANNEL, "message_ts": PARENT},
                slack_response("references_thread.txt"),
            ),
            ("slack_read_canvas", {"canvas_id": CANVAS}, [{"type": "text", "text": canvas_text}]),
        ]
        slack_run(config, when, reads)
        return build(config)

    return run


def edited(tmp_path, fixture: str, old: str, new: str) -> str:
    """An edited copy of a fixture, outside the fixtures folder."""
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / f"edited-{len(list(tmp_path.glob('edited-*')))}-{fixture}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return str(path)


def log_file(config) -> Path:
    return config.data_dir / "references" / "references.jsonl"


def view_file(config) -> Path:
    return config.data_dir / "references" / "references.md"


def log(config) -> dict[str, dict]:
    lines = log_file(config).read_text(encoding="utf-8").split("\n")
    return {entry["key"]: entry for entry in map(json.loads, filter(None, lines))}


def states(config) -> dict[str, str]:
    return {key: entry["state"] for key, entry in log(config).items()}


# Finding links


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "<https://example.slack.com/docs/T00000000/F00000010|Plan>",
            ("slack-file:F00000010", "slack-canvas"),
        ),
        (
            "<https://example.slack.com/files/U00000002/F00000011/review.pdf>",
            ("slack-file:F00000011", "slack-attachment"),
        ),
        (
            "<https://files.slack.com/files-pri/T00000000-F00000011/review.pdf>",
            ("slack-file:F00000011", "slack-attachment"),
        ),
        (
            "<https://docs.google.com/document/d/FAKE-DOC-0010/edit#heading=h.fake01>",
            (DOC, "google-doc"),
        ),
        ("<https://docs.google.com/document/u/1/d/FAKE-DOC-0010/edit>", (DOC, "google-doc")),
        (
            "<https://docs.google.com/spreadsheets/d/FAKE-SHEET-0010/edit#gid=0>",
            (SHEET, "google-sheet"),
        ),
        (
            "<https://docs.google.com/presentation/d/FAKE-SLIDES-0010/edit>",
            (SLIDES, "google-slides"),
        ),
        (
            "<https://drive.google.com/file/d/FAKE-FILE-0010/view?usp=sharing>",
            ("google:FAKE-FILE-0010", "google-file"),
        ),
        # A folder: approving it wouldn't be approving its files.
        (
            "<https://drive.google.com/drive/folders/FAKE-FOLDER-0010>",
            ("url:https://drive.google.com/drive/folders/FAKE-FOLDER-0010", "link"),
        ),
        # A published doc's link doesn't name the doc's own ID.
        (
            "<https://docs.google.com/document/d/e/FAKE-PUBLISHED-0010/pub>",
            ("url:https://docs.google.com/document/d/e/FAKE-PUBLISHED-0010/pub", "link"),
        ),
        (
            "<https://example.slack.com/archives/C00000001/p1791295200000100>",
            ("url:https://example.slack.com/archives/C00000001/p1791295200000100", "link"),
        ),
        # Slack writes & as &amp;. The key keeps the query and drops the # part.
        (
            "<https://example.org/paper?id=7&amp;v=2#results|A paper>",
            ("url:https://example.org/paper?id=7&v=2", "link"),
        ),
        (
            "[Draft plan](https://docs.google.com/document/d/FAKE-DOC-0010/edit)",
            (DOC, "google-doc"),
        ),  # canvas text
        ("Reading: https://example.org/paper?id=7.", (PAPER, "link")),  # written out
    ],
)
def test_each_kind_of_link_is_found_and_typed(text, expected):
    assert [(found.key, found.type) for found in in_text(text)] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "<mailto:riley@example.com|riley@example.com>",
        "Write to riley@example.com",
        "<@U00000001>, <#C00000001|example-channel> and <!here>",
        "Line one<br>line two",
    ],
)
def test_emails_mentions_and_html_are_not_links(text):
    assert in_text(text) == []


def test_link_text_is_kept_unless_it_only_repeats_the_address():
    found = in_text("<https://example.org/a|Notes> <https://example.org/b|https://example.org/b>")
    assert [found.title for found in found] == ["Notes", None]


def test_a_less_than_sign_in_canvas_text_doesnt_hide_a_link():
    found = in_text("If a < b see https://example.org/x and c > d")
    assert [found.key for found in found] == ["url:https://example.org/x"]


def test_a_link_never_holds_a_line_break():
    found = in_text("<https://example.org/a\r- Key: fake|Notes>")
    assert [found.link for found in found] == ["https://example.org/a"]


# The log


def test_each_source_appears_once_with_its_type(config, export):
    export()
    assert {key: entry["type"] for key, entry in log(config).items()} == EVERY_SOURCE


def test_one_doc_linked_from_a_post_a_reply_and_a_canvas_is_one_source(config, export):
    export()
    entry = log(config)[DOC]
    # The post is in both the channel read and the thread read, and is still one place.
    assert [place["place"] for place in entry["places"]] == [
        f"message:{CHANNEL}:{PARENT}",
        f"message:{CHANNEL}:{REPLY}",
        f"canvas:{CANVAS}",
    ]
    post, reply, canvas = entry["places"]
    assert post["who"] == {"id": "U00000001", "name": "Alex Example"}
    assert post["at"] == "2026-10-06T14:00:00Z" and post["via"] == ["link"]
    assert reply["thread_ts"] == PARENT and reply["link"].endswith(
        f"?thread_ts={PARENT}&cid={CHANNEL}"
    )
    assert canvas["canvas_id"] == CANVAS and canvas["at"] == "2026-10-08T12:03:00Z"
    assert entry["title"] == "Draft plan" and entry["first_seen"] == "2026-10-06T14:00:00Z"
    assert all(place["latest"] for place in entry["places"])


def test_an_attachment_is_logged_with_its_name_type_poster_and_time(config, export):
    export()
    entry = log(config)["slack-file:F00000012"]
    assert (entry["title"], entry["file_type"]) == ("meeting-recording.mp4", "video/mp4")
    [place] = entry["places"]
    assert place["who"] == {"id": "U00000002", "name": "Sam Sample"}
    assert (place["at"], place["via"]) == ("2026-10-07T15:00:00Z", ["attachment"])
    assert (entry["state"], entry["reason"]) == (
        "not-collected",
        "attachments are never downloaded",
    )
    # Logged, never downloaded: nothing in the data folder is named for it.
    assert not any("meeting-recording" in p.name for p in config.data_dir.rglob("*"))


def test_an_attachment_whose_type_says_canvas_is_a_canvas(config, export):
    export()
    entry = log(config)["slack-file:F00000013"]
    assert (entry["type"], entry["title"], entry["state"]) == ("slack-canvas", "Team notes", "new")


def test_a_tab_notice_names_a_canvas(config, export):
    export()
    entry = log(config)["slack-file:F00000014"]
    [place] = entry["places"]
    assert entry["type"] == "slack-canvas" and place["via"] == ["tab notice"]
    assert place["who"] == {"id": "USLACKBOT", "name": "Slackbot"}


def test_a_canvas_title_comes_from_its_read_not_the_link_text(config, export):
    export()
    assert log(config)["slack-file:F00000010"]["title"] == "Example Plan Canvas"


def test_a_link_edited_out_of_a_message_is_kept_and_marked(config, export, tmp_path):
    export()
    export(
        T0 + timedelta(days=1), edited(tmp_path, "references_channel.txt", SHEET_LINK, "the sheet")
    )
    [place] = log(config)[SHEET]["places"]
    assert place["latest"] is False
    assert states(config)[SHEET] == "taken-back"  # every link to it was edited out
    assert states(config)[SLIDES] == "new"  # still in the same message


def test_no_link_then_link_then_no_link_is_marked_as_edited_out(config, export, tmp_path):
    without = edited(tmp_path, "references_channel.txt", SHEET_LINK, "the sheet")
    export(T0, without)
    export(T0 + timedelta(days=1))
    export(T0 + timedelta(days=2), without)
    # The store drops the third version, since it matches the first, so its newest line
    # still has the link. The latest saved read doesn't.
    store = config.data_dir / "slack" / "store" / CHANNEL / "messages.jsonl"
    versions = [r for r in map(json.loads, store.read_text(encoding="utf-8").splitlines())]
    assert "FAKE-SHEET-0010" in [r for r in versions if r["ts"] == "1791298800.000300"][-1]["text"]
    [place] = log(config)[SHEET]["places"]
    assert place["latest"] is False
    assert states(config)[SHEET] == "taken-back"


def test_a_link_edited_out_of_a_canvas_is_kept_and_marked(config, export, tmp_path):
    export()
    export(
        T0 + timedelta(days=1), canvas=edited(tmp_path, "references_canvas.json", PAPER_LINE, "")
    )
    message, canvas = log(config)[PAPER]["places"]
    assert message["latest"] is True and canvas["latest"] is False
    assert canvas["at"] == "2026-10-08T12:03:00Z"  # when the canvas first had it


def test_a_mark_stays_when_the_inbox_no_longer_has_the_message(config, export, tmp_path):
    without = edited(tmp_path, "references_channel.txt", SHEET_LINK, "the sheet")
    export(T0, without)
    export(T0 + timedelta(days=1))
    export(T0 + timedelta(days=2), without)
    # The store's newest version of the message still has the link, so it mustn't decide.
    shutil.rmtree(config.data_dir / "slack" / "inbox" / CHANNEL)
    build(config)
    [place] = log(config)[SHEET]["places"]
    assert place["latest"] is False
    assert states(config)[SHEET] == "taken-back"


def test_a_new_read_without_the_link_updates_a_kept_place(config, export, tmp_path):
    export()
    # The saved data behind the references is deleted, then read again without the links.
    shutil.rmtree(config.data_dir / "slack" / "store" / CHANNEL)
    shutil.rmtree(config.data_dir / "slack" / "inbox" / CHANNEL)
    shutil.rmtree(config.data_dir / "documents")
    export(
        T0 + timedelta(days=1),
        edited(tmp_path, "references_channel.txt", SHEET_LINK, "the sheet"),
        edited(tmp_path, "references_canvas.json", PAPER_LINE, ""),
    )
    [place] = log(config)[SHEET]["places"]
    assert place["latest"] is False and states(config)[SHEET] == "taken-back"
    message, canvas = log(config)[PAPER]["places"]
    assert message["latest"] is True and canvas["latest"] is False


def test_a_title_with_a_unicode_line_separator_doesnt_break_the_log(config, export, tmp_path):
    # JSON keeps U+2028 and U+2029 as they are, and Python's splitlines() splits at them.
    export(
        canvas=edited(
            tmp_path, "references_canvas.json", "![chart.png]", "![chart\\u2028one\\u2029.png]"
        )
    )
    assert log(config)["slack-file:F00000015"]["title"] == "chart one .png"
    report = build(config)
    assert "Couldn't read the reference log" not in report and "0 added this build" in report


# States


def test_the_states_after_one_export(config, export):
    export()
    assert states(config) == {
        key: "not-collected" if kind in ("slack-attachment", "link") else "new"
        for key, kind in EVERY_SOURCE.items()
    } | {"slack-file:F00000010": "collected"}
    assert log(config)[PAPER]["reason"] == "ordinary links are logged only"


def test_a_listed_canvas_not_read_yet_is_approved(config, export):
    export()
    build(replace(config, slack_canvases=(CANVAS, "F00000014")))
    assert states(config)["slack-file:F00000014"] == "approved"


def rewrite_log(config, key: str, change) -> None:
    """Change one source in the log by hand, as a fetcher or a damaged file might."""
    entries = list(log(config).values())
    for entry in entries:
        if entry["key"] == key:
            change(entry)
    log_file(config).write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_a_listed_source_a_fetcher_couldnt_reach_says_why(config, export):
    export()
    reason = "the connector said it doesn't exist"
    rewrite_log(config, "slack-file:F00000014", lambda entry: entry.update(unreachable=reason))
    build(replace(config, slack_canvases=(CANVAS, "F00000014")))
    entry = log(config)["slack-file:F00000014"]
    assert (entry["state"], entry["reason"]) == (
        "unreachable",
        "the connector said it doesn't exist",
    )
    assert "diffdata can't reach it: the connector said it doesn't exist." in view_file(
        config
    ).read_text(encoding="utf-8")


def test_a_canvas_collected_or_listed_before_but_unlisted_now_is_withdrawn(config, export):
    export()
    build(replace(config, slack_canvases=(CANVAS, "F00000014")))
    build(replace(config, slack_canvases=()))
    assert states(config)["slack-file:F00000010"] == "withdrawn"  # it was read
    assert states(config)["slack-file:F00000014"] == "withdrawn"  # it was listed


def test_an_attachment_listed_in_config_becomes_a_canvas_for_good(config, export):
    export()
    build(replace(config, slack_canvases=(CANVAS, "F00000012")))
    entry = log(config)["slack-file:F00000012"]
    assert (entry["type"], entry["state"]) == ("slack-canvas", "approved")
    build(config)
    entry = log(config)["slack-file:F00000012"]
    assert (entry["type"], entry["state"]) == ("slack-canvas", "withdrawn")


def test_a_slack_file_with_saved_canvas_reads_is_a_canvas(config, export):
    export()
    read = {"canvas_id": "F00000011", "markdown_content": "# Review notes\n"}
    record = {
        "saved_at": "2026-10-08T13:00:00.000000Z",
        "tool_response": [{"type": "text", "text": json.dumps(read)}],
    }
    slack_canvas.save_read(config.data_dir, "20261008T130000Z", "F00000011", record, "toolu_x")
    build(config)
    entry = log(config)["slack-file:F00000011"]
    assert (entry["type"], entry["title"], entry["state"]) == (
        "slack-canvas",
        "Review notes",
        "withdrawn",  # read before, and not listed now
    )


def test_a_canvas_with_an_unreadable_read_keeps_its_entries(config, export, tmp_path):
    export()
    raw = config.data_dir / "documents" / "slack-canvas" / CANVAS / "raw" / "20261008T130000Z"
    raw.mkdir(parents=True)
    (raw / "slack_read_canvas-broken.json").write_text("not JSON", encoding="utf-8")
    # This read drops the paper link, but the canvas can't be built, so its entries stay.
    export(
        T0 + timedelta(days=1), canvas=edited(tmp_path, "references_canvas.json", PAPER_LINE, "")
    )
    _, canvas = log(config)[PAPER]["places"]
    assert canvas["latest"] is True
    assert log(config)["slack-file:F00000010"]["title"] == "Example Plan Canvas"


def test_a_source_linked_only_from_places_no_longer_opted_in_is_history_only(config, export):
    export()
    build(replace(config, slack_channels=("other-channel",), slack_canvases=()))
    # The doc is linked from the channel and from the canvas, neither opted in now.
    assert states(config)[DOC] == "history-only"
    assert states(config)["slack-file:F00000013"] == "history-only"


def test_the_log_only_grows(config, export):
    export()
    before = log(config)
    shutil.rmtree(config.data_dir / "slack" / "store" / CHANNEL)
    shutil.rmtree(config.data_dir / "slack" / "inbox" / CHANNEL)
    shutil.rmtree(config.data_dir / "documents")
    build(config)
    after = log(config)
    assert after.keys() == before.keys()
    # Every place stays. Only the canvas's state changes: its reads are gone.
    assert {k: e["places"] for k, e in after.items()} == {k: e["places"] for k, e in before.items()}
    assert after["slack-file:F00000010"]["state"] == "approved"
    assert after["slack-file:F00000010"]["title"] == "Example Plan Canvas"


@pytest.mark.parametrize("damage", ["not JSON", '{"key": "url:https://example.org/"}', "[]"])
def test_a_log_diffdata_cant_read_isnt_overwritten(config, export, damage):
    export()
    with log_file(config).open("a", encoding="utf-8") as f:
        f.write(damage + "\n")
    before = (log_file(config).read_bytes(), view_file(config).read_bytes())
    report = build(config)
    assert (
        "Couldn't read the reference log, so it was left as it was, with nothing added." in report
    )
    assert report.splitlines()[0].startswith("1 channel(s)")  # the rest of the build finished
    assert (log_file(config).read_bytes(), view_file(config).read_bytes()) == before


@pytest.mark.parametrize("field, value", [("who", "Alex"), ("via", [1]), ("latest", "yes")])
def test_a_log_line_with_a_damaged_field_isnt_overwritten(config, export, field, value):
    export()
    rewrite_log(config, DOC, lambda entry: entry["places"][0].update({field: value}))
    before = log_file(config).read_bytes()
    assert "Couldn't read the reference log" in build(config)
    assert log_file(config).read_bytes() == before


def test_a_log_diffdata_cant_write_is_reported(config, export, monkeypatch):
    def refuse(path, text):
        raise PermissionError("synthetic: the file is open elsewhere")

    monkeypatch.setattr("diffdata.discover.references.write_log", refuse)
    report = export()
    assert "Couldn't write the reference log, so it was left as it was." in report
    assert report.splitlines()[-1].startswith("Closed export run")  # the build finished


def test_building_twice_changes_nothing(config, export):
    export()
    before = (log_file(config).read_bytes(), view_file(config).read_bytes())
    assert "0 added this build" in build(config)
    assert (log_file(config).read_bytes(), view_file(config).read_bytes()) == before


def test_the_report_gives_counts_only(export):
    report = export()
    assert (
        "Reference log: 14 source(s) from 17 place(s), 14 added this build. 3 canvas(es), "
        "4 Google file(s), 3 attachment(s), 4 other link(s)."
    ) in report
    assert (
        "6 canvas(es) or Google file(s) aren't listed in config.toml: see "
        "references/references.md in your data folder."
    ) in report
    for private in ("FAKE-", "F0000", "U0000", "Draft", "Example Plan", "Sam", "Slackbot", "http"):
        assert private not in report


def test_the_view_matches_the_expected_markdown(config, export):
    export()
    expected = (FIXTURES / "references_expected.md").read_text(encoding="utf-8")
    actual = view_file(config).read_text(encoding="utf-8")
    assert actual.replace(f"diffdata {__version__}", "diffdata VERSION") == expected


def test_a_title_or_name_cant_start_a_new_line_in_the_view():
    place = {
        "place": f"message:{CHANNEL}:{PARENT}",
        "channel_id": CHANNEL,
        "ts": PARENT,
        "thread_ts": None,
        "canvas_id": None,
        "link": None,
        "who": {"id": "U00000001", "name": "Alex\n- 2026-10-01 00:00 UTC · fake place"},
        "at": "2026-10-06T14:00:00Z",
        "via": ["link"],
        "latest": True,
    }
    entry = {
        "key": "url:https://example.org/",
        "type": "link",
        "state": "not-collected",
        "reason": "ordinary links are logged only",
        "title": "Notes\r\n### b) Fake entry",
        "link": "https://example.org/",
        "file_type": None,
        "first_seen": "2026-10-06T14:00:00Z",
        "unreachable": None,
        "places": [place],
    }
    lines = render([entry], {CHANNEL: "example-channel"}, {}).splitlines()
    assert "### Notes ### b) Fake entry" in lines
    assert not any(line.startswith(("### b)", "- 2026-10-01")) for line in lines)
