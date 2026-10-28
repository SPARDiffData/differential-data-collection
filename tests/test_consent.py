"""Approving, skipping and withdrawing the canvases and Google files the reference log finds,
typed in a terminal (CON-1, CON-3). One test or more for each item on the must-refuse list
in the plan on #65. Synthetic fixtures only."""

import io
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata.cli import main
from diffdata.common.config import Config
from diffdata.consent import approvals, review
from diffdata.discover.slack_export import build, canvases_due
from diffdata.discover.slack_inbox import begin, check, open_run, save

CHANNEL = "C00000001"
PARENT = "1791295200.000100"
CANVAS = "F00000010"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=1)
FIXTURES = Path(__file__).parent / "fixtures" / "slack"
DOC = "google:FAKE-DOC-0010"
SHEET = "google:FAKE-SHEET-0010"
SLIDES = "google:FAKE-SLIDES-0010"
DRIVE = "google:FAKE-FILE-0010"
TEAM_NOTES = "slack-file:F00000013"
TAB = "slack-file:F00000014"
SHEET_LINK = "<https://docs.google.com/spreadsheets/d/FAKE-SHEET-0010/edit#gid=0|Budget sheet>"
# The list, oldest first: a) the doc, b) the sheet, c) the slides, d) the Drive file,
# e) the canvas shared as an attachment, f) the canvas a tab notice names.
LISTED = [DOC, SHEET, SLIDES, DRIVE, TEAM_NOTES, TAB]
# Nothing identifying may reach Claude's chat: the build's output and every error message.
PRIVATE = ("FAKE-", "F0000", "U0000", "Draft", "Budget", "Team notes", "Sam", "http")


@pytest.fixture
def config(tmp_path):
    config_file = tmp_path / "home" / ".diffdata" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("# synthetic: the tests pass their settings directly\n")
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
        slack_canvases=(CANVAS,),
        path=config_file,
    )


@pytest.fixture
def export(config, slack_run, slack_response):
    """One /slack-export run, then the build: the channel, its thread and the listed canvas."""

    def run(when=T0, channel="references_channel.txt", settings=None):
        settings = settings or config
        canvas_text = (FIXTURES / "references_canvas.json").read_text(encoding="utf-8")
        reads = [
            ("slack_read_channel", {"channel_id": CHANNEL}, slack_response(channel)),
            (
                "slack_read_thread",
                {"channel_id": CHANNEL, "message_ts": PARENT},
                slack_response("references_thread.txt"),
            ),
            ("slack_read_canvas", {"canvas_id": CANVAS}, [{"type": "text", "text": canvas_text}]),
        ]
        slack_run(settings, when, reads)
        return build(settings)

    return run


@pytest.fixture
def terminal(monkeypatch):
    """A person at a terminal. The tests of the check itself don't use this."""
    monkeypatch.setattr(review, "is_terminal", lambda stream: True)


class Typed(io.StringIO):
    """What the person types. `meanwhile` runs while the list is on screen, before the
    answer arrives, as another command might."""

    def __init__(self, text: str, meanwhile=None):
        super().__init__(text)
        self.meanwhile = meanwhile

    def readline(self, *args):
        if self.meanwhile:
            self.meanwhile()
        return super().readline(*args)


class CtrlC(io.StringIO):
    def readline(self, *args):
        raise KeyboardInterrupt


def answer(config, text, step="review", meanwhile=None, settings=None):
    """Run review or withdraw with this answer. Returns the exit code and the screen."""
    screen = io.StringIO()
    run = review.run_review if step == "review" else review.run_withdraw
    stdin = text if isinstance(text, io.StringIO) else Typed(text, meanwhile)
    code = run(settings or config, stdin, screen, now=T0 + timedelta(hours=1))
    return code, screen.getvalue()


def approvals_file(config) -> Path:
    return config.path.parent / "approvals.jsonl"


def decisions(config) -> dict[str, str]:
    return approvals.read_decisions(approvals_file(config))


def items(screen: str) -> list[str]:
    return re.findall(r"^[a-z]+\) .*$", screen, re.MULTILINE)


def log(config) -> dict[str, dict]:
    path = config.data_dir / "references" / "references.jsonl"
    lines = path.read_text(encoding="utf-8").split("\n")
    return {entry["key"]: entry for entry in map(json.loads, filter(None, lines))}


def edited(tmp_path, fixture: str, old: str, new: str) -> str:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / f"edited-{len(list(tmp_path.glob('edited-*')))}-{fixture}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return str(path)


# The review screen


def test_the_screen_says_what_approving_means_then_lists_one_line_per_item(
    config, export, terminal
):
    export()
    code, screen = answer(config, "\n")
    assert code == 0
    for text in (
        "What approving means:",
        "reads an approved item on every /slack-export from now on, and keeps every",
        "Claude reads an approved canvas in full",
        "Approve only what your team agreed to share. A doc others wrote may need their",
        "A skipped item isn't asked about again.",
    ):
        assert text in screen
    lines = items(screen)
    assert [line[:2] for line in lines] == ["a)", "b)", "c)", "d)", "e)", "f)"]
    assert lines[0] == (
        "a) Google Doc · https://docs.google.com/document/d/FAKE-DOC-0010/edit#heading=h.fake01"
        ' · "Draft plan" · first referenced 2026-10-06 in #example-channel'
    )
    assert lines[5] == (
        "f) Slack canvas · ID F00000014, with no link in the saved data"
        " · first referenced 2026-10-07 in #example-channel"
    )


def test_attachments_and_ordinary_links_are_never_on_the_list(config, export, terminal):
    # Must refuse 7: the fixture also holds 3 attachments and 4 ordinary links.
    export()
    _, screen = answer(config, "all\n")
    assert sorted(decisions(config)) == sorted(LISTED)
    for kept_out in ("review.pdf", "example.org", "code.example.com", "folders", "archives"):
        assert kept_out not in screen


def test_a_canvas_id_too_short_to_be_real_isnt_offered(config, export, terminal, tmp_path):
    # From the review: the approvals file takes only real-looking keys, so answering about
    # this one would write a line diffdata can't read back.
    short = SHEET_LINK + " <https://example.slack.com/docs/T00000000/F12345>"
    export(T0, edited(tmp_path, "references_channel.txt", SHEET_LINK, short))
    assert log(config)["slack-file:F12345"]["state"] == "new"
    _, screen = answer(config, "all\n")
    assert "F12345" not in screen
    assert sorted(decisions(config)) == sorted(LISTED)


def test_letters_go_past_z():
    assert review.letters(28)[:2] == ["a", "b"]
    assert review.letters(28)[-3:] == ["z", "aa", "ab"]


# The answers


@pytest.mark.parametrize(
    "typed, skipped",
    [
        ("all", []),
        ("none", LISTED),
        ("all but b, d", [SHEET, DRIVE]),
        ("all but b,d", [SHEET, DRIVE]),
        ("  ALL  but  b   d ", [SHEET, DRIVE]),
    ],
)
def test_each_answer_records_the_right_decisions(config, export, terminal, typed, skipped):
    export()
    code, screen = answer(config, typed + "\n")
    assert code == 0
    assert decisions(config) == {key: "skipped" if key in skipped else "approved" for key in LISTED}
    assert f"Recorded: {6 - len(skipped)} approved, {len(skipped)} skipped." in screen
    rows = [json.loads(line) for line in approvals_file(config).read_text().split("\n") if line]
    assert {row["answer"] for row in rows} == {" ".join(typed.lower().split())}
    assert {row["at"] for row in rows} == {"2026-10-08T13:00:00Z"}


def test_the_approvals_file_holds_keys_decisions_times_and_answers_only(config, export, terminal):
    # Must refuse 14: no titles, names or link text in the approvals file.
    export()
    answer(config, "all but a\n")
    text = approvals_file(config).read_text(encoding="utf-8")
    for line in filter(None, text.split("\n")):
        assert sorted(json.loads(line)) == ["answer", "at", "decision", "key"]
    for private in ("Draft", "Budget", "Team notes", "Sam", "example-channel", "http"):
        assert private not in text


@pytest.mark.parametrize(
    "typed", ["yes", "a, b", "all but z", "all but", "all but b,", "approve all", "all, but b"]
)
def test_an_answer_review_doesnt_take_records_nothing(config, export, terminal, typed):
    # Must refuse 2.
    export()
    code, screen = answer(config, typed + "\n")
    assert code == 1
    assert "That isn't an answer review takes, so nothing was recorded." in screen
    assert "Type all, none, or all but and the letters to skip, such as: all but b, d" in screen
    assert not approvals_file(config).exists()


@pytest.mark.parametrize("typed", ["\n", "   \n", "", CtrlC()])
def test_no_answer_approves_nothing_and_the_items_stay_on_the_list(config, export, terminal, typed):
    # Must refuse 4: an empty line, closed input ("" is end of input) or Ctrl+C.
    export()
    code, screen = answer(config, typed)
    assert code == 0
    assert "No answer, so nothing was recorded." in screen
    assert not approvals_file(config).exists()
    assert len(items(answer(config, "\n")[1])) == 6
    build(config)
    assert {log(config)[key]["state"] for key in LISTED} == {"new"}


# Not a terminal


def test_review_and_withdraw_refuse_when_they_arent_in_a_terminal(config, export, capsys):
    # Must refuse 1: Claude's tools give a command neither a terminal to read nor to show.
    export()
    approvals_file(config).write_text(
        json.dumps(
            {"key": TAB, "decision": "approved", "at": "2026-10-08T12:30:00Z", "answer": "all"}
        )
        + "\n"
    )
    before = approvals_file(config).read_bytes()
    for step in ("review", "withdraw"):
        code, screen = answer(config, "all\n", step)
        assert code == 1 and screen == ""
        err = capsys.readouterr().err
        assert f"diffdata consent {step} only runs in a terminal you type in" in err
        assert not any(private in err for private in PRIVATE)
    assert approvals_file(config).read_bytes() == before


def test_the_null_device_isnt_a_terminal():
    # On Windows, isatty() is true for NUL, where Claude Code's tools point a command's input.
    with open(os.devnull) as null:
        assert not review.is_terminal(null)
    assert not review.is_terminal(io.StringIO("all\n"))


def test_the_command_refuses_without_a_terminal(config, export, no_real_config, capsys):
    export()
    no_real_config.parent.mkdir(parents=True, exist_ok=True)
    data = config.data_dir.as_posix()
    no_real_config.write_text(f'[local]\ndata_dir = "{data}"\n[sources]\nslack_channels = []\n')
    assert main(["consent", "review"]) == 1
    assert "only runs in a terminal you type in" in capsys.readouterr().err
    assert not (no_real_config.parent / "approvals.jsonl").exists()


def test_outside_a_terminal_a_config_error_shows_nothing_of_the_config(no_real_config, capsys):
    # Must refuse 14: config.toml's errors can quote its text and paths, so the terminal
    # check comes first.
    no_real_config.parent.mkdir(parents=True, exist_ok=True)
    no_real_config.write_text("[FAKE-PRIVATE-DOC]\n[FAKE-PRIVATE-DOC]\n")
    for step in ("review", "withdraw"):
        assert main(["consent", step]) == 1
        err = capsys.readouterr().err
        assert "only runs in a terminal you type in" in err
        assert "FAKE-PRIVATE-DOC" not in err and str(no_real_config.parent) not in err


# Changes while the list is shown


def test_a_build_while_the_list_is_shown_records_nothing(config, export, terminal, capsys):
    # Must refuse 3: the build changes what's listed, so the letters may mean something else.
    export()
    listed_now = replace(config, slack_canvases=(CANVAS, "F00000014"))
    code, _ = answer(config, "all\n", meanwhile=lambda: build(listed_now))
    assert code == 1
    assert "Something changed while the list was shown" in capsys.readouterr().err
    assert not approvals_file(config).exists()


def test_another_review_while_the_list_is_shown_records_nothing(config, export, terminal, capsys):
    export()

    def another_review():
        approvals.record(approvals_file(config), [(SHEET, "skipped")], "none", T0)

    code, _ = answer(config, "all\n", meanwhile=another_review)
    assert code == 1
    assert "Something changed while the list was shown" in capsys.readouterr().err
    assert decisions(config) == {SHEET: "skipped"}


def test_a_withdrawal_while_the_withdraw_list_is_shown_records_nothing(
    config, export, terminal, capsys
):
    export()
    answer(config, "all\n")

    def another_withdrawal():
        approvals.record(approvals_file(config), [(TAB, "withdrawn")], "f", T0)

    code, _ = answer(config, "all\n", "withdraw", meanwhile=another_withdrawal)
    assert code == 1
    assert "Something changed while the list was shown" in capsys.readouterr().err
    assert list(decisions(config).values()).count("withdrawn") == 1


@pytest.mark.parametrize("step", ["review", "withdraw"])
def test_an_answer_recorded_just_before_this_one_is_never_undone(
    config, export, terminal, monkeypatch, capsys, step
):
    # Must refuse 3, from Codex's review: another answer recorded after the list's last check
    # but before this write would be lost when this one writes its copy. So the last check
    # comes after taking hold of the file. Here the other answer lands just before that.
    export()
    if step == "withdraw":
        answer(config, "all\n")
    hold = approvals.locked

    def another_answer_first(path):
        approvals.record(path, [(TAB, "withdrawn")], "f", T0)
        return hold(path)

    monkeypatch.setattr(approvals, "locked", another_answer_first)
    assert answer(config, "all\n", step)[0] == 1
    assert "Something changed while the list was shown" in capsys.readouterr().err
    assert decisions(config)[TAB] == "withdrawn"


def test_an_answer_isnt_recorded_while_another_is_being_recorded(config, export, terminal, capsys):
    export()
    lock = approvals_file(config).with_name("approvals.jsonl.lock")
    lock.write_text("")
    assert answer(config, "all\n")[0] == 1
    assert "Another review or withdraw is recording its answer right now" in capsys.readouterr().err
    assert not approvals_file(config).exists()
    assert lock.exists()  # someone else's: left alone
    lock.unlink()
    assert answer(config, "all\n")[0] == 0
    assert not lock.exists()  # this one's own: removed once recorded


# What the export reads


def test_an_approved_canvas_is_in_the_next_run_and_read_as_never_read(config, export, terminal):
    export()
    answer(config, "all\n")
    report = begin(config, [f"example-channel={CHANNEL}"], now=LATER)
    assert "Export run 20261009T120000Z is open for 1 channel(s) and 3 canvas(es)." in report
    assert "2 of the canvases were approved at review" in report
    assert open_run(config.data_dir).canvases == [CANVAS, "F00000013", "F00000014"]
    due = canvases_due(config, now=LATER + timedelta(minutes=1))
    assert due.startswith("3 of 3 listed or approved canvas(es) to read.")
    # The two approved canvases. The listed one was read before, but this run has no search.
    assert "2 never read, 1 not in this run's files search." in due
    assert due.endswith(f"Canvases to read: {CANVAS}, F00000013, F00000014")


@pytest.mark.parametrize("decision", ["skipped", "withdrawn", None])
def test_a_canvas_neither_listed_nor_approved_is_stopped_and_not_saved(
    config, export, slack_hook, decision
):
    # Must refuse 5: skipped, withdrawn, or new with no decision yet.
    export()
    if decision:
        approvals.record(approvals_file(config), [(TEAM_NOTES, "approved")], "all", T0)
        approvals.record(approvals_file(config), [(TEAM_NOTES, decision)], "x", T0)
    begin(config, [f"example-channel={CHANNEL}"], now=LATER)
    assert open_run(config.data_dir).canvases == [CANVAS]
    read = slack_hook("slack_read_canvas", {"canvas_id": "F00000013"}, None)
    assert check(config, read, now=LATER) == 2
    text = json.dumps({"canvas_id": "F00000013", "markdown_content": "# Team notes\n"})
    result = slack_hook(
        "slack_read_canvas", {"canvas_id": "F00000013"}, [{"type": "text", "text": text}]
    )
    assert save(config, result, now=LATER) == 2
    assert not (config.data_dir / "documents" / "slack-canvas" / "F00000013").exists()


def test_a_history_only_canvas_isnt_read(config, export, slack_hook):
    export()
    elsewhere = replace(config, slack_channels=("other-channel",), slack_canvases=())
    begin(replace(elsewhere, slack_channels=("example-channel",)), [f"example-channel={CHANNEL}"])
    read = slack_hook("slack_read_canvas", {"canvas_id": "F00000013"}, None)
    assert check(config, read) == 2


# Every Slack read the connector offered on 2026-10-09 that the export never makes.
UNUSED_READS = [
    "slack_get_reactions",
    "slack_list_channel_members",
    "slack_list_user_channels",
    "slack_read_file",
    "slack_read_list",
    "slack_read_user_profile",
    "slack_search_emojis",
    "slack_search_public",
    "slack_search_users",
    "slack_some_tool_added_later",
]


@pytest.mark.parametrize("tool", UNUSED_READS)
def test_a_slack_read_the_export_doesnt_use_is_stopped(config, export, slack_hook, capsys, tool):
    # Must refuse 6: even with a run open, and even for a file the run's channel holds.
    export()
    begin(config, [f"example-channel={CHANNEL}"], now=LATER)
    payload = slack_hook(tool, {"file_id": "F00000011", "channel_id": CHANNEL}, None)
    assert check(config, payload, now=LATER) == 2
    assert f"/slack-export never uses {tool}, so it was stopped before it ran." in (
        capsys.readouterr().err
    )


def test_finding_a_channels_id_goes_ahead_before_the_run_opens(config, slack_hook):
    payload = slack_hook("slack_search_channels", {"keywords": "example-channel"}, None)
    assert open_run(config.data_dir) is None
    assert check(config, payload) == 0


# What's never asked about again


def test_a_skipped_item_isnt_asked_about_again_even_when_linked_again(
    config, export, terminal, tmp_path
):
    # Must refuse 8.
    export()
    answer(config, "all but a\n")
    again = (
        SHEET_LINK + " and the plan again: <https://docs.google.com/document/d/FAKE-DOC-0010/edit>"
    )
    export(LATER, edited(tmp_path, "references_channel.txt", SHEET_LINK, again))
    assert len(log(config)[DOC]["places"]) == 4  # one more place than before
    assert log(config)[DOC]["state"] == "declined"
    assert answer(config, "\n")[1] == "Nothing is waiting for review.\n"


def test_a_source_only_from_places_no_longer_opted_in_isnt_listed(config, export, terminal):
    # Must refuse 9: the channel left config.toml, and so did the canvas linking the doc.
    export()
    elsewhere = replace(config, slack_channels=("other-channel",), slack_canvases=())
    assert answer(config, "\n", settings=elsewhere)[1] == "Nothing is waiting for review.\n"


def test_a_canvas_no_longer_approved_stops_counting_as_a_place(config, export, terminal):
    # The doc is linked from the channel and from the canvas: with the channel gone and the
    # canvas approved instead of listed, the canvas's link still counts; withdrawn, it doesn't.
    export()
    approvals.record(approvals_file(config), [(f"slack-file:{CANVAS}", "approved")], "all", T0)
    elsewhere = replace(config, slack_channels=("other-channel",), slack_canvases=())
    _, screen = answer(config, "\n", settings=elsewhere)
    assert [line[:16] for line in items(screen)] == ["a) Google Doc · "]
    approvals.record(approvals_file(config), [(f"slack-file:{CANVAS}", "withdrawn")], "a", T0)
    assert answer(config, "\n", settings=elsewhere)[1] == "Nothing is waiting for review.\n"


def test_a_source_with_no_link_left_in_any_latest_read_isnt_listed(
    config, export, terminal, tmp_path
):
    # Must refuse 10: no link, then link, then no link.
    without = edited(tmp_path, "references_channel.txt", SHEET_LINK, "the sheet")
    export(T0, without)
    export(T0 + timedelta(days=1))
    export(T0 + timedelta(days=2), without)
    _, screen = answer(config, "\n")
    assert len(items(screen)) == 5
    assert "FAKE-SHEET" not in screen


# Withdraw


def test_a_withdrawn_canvas_isnt_read_from_the_next_run_or_asked_about_again(
    config, export, terminal, slack_hook
):
    # Must refuse 11. Approved, sorted by key: a) to d) the Google files, e) and f) canvases.
    export()
    answer(config, "all\n")
    code, screen = answer(config, "e\n", "withdraw")
    assert code == 0
    assert "What was already collected stays in your data" in screen
    assert "Withdrawn: 1. diffdata stops reading them from the next /slack-export." in screen
    assert decisions(config)[TEAM_NOTES] == "withdrawn"
    begin(config, [f"example-channel={CHANNEL}"], now=LATER)
    assert open_run(config.data_dir).canvases == [CANVAS, "F00000014"]
    read = slack_hook("slack_read_canvas", {"canvas_id": "F00000013"}, None)
    assert check(config, read, now=LATER) == 2
    build(config)
    assert log(config)[TEAM_NOTES]["state"] == "withdrawn"
    assert answer(config, "\n")[1] == "Nothing is waiting for review.\n"
    assert len(items(answer(config, "\n", "withdraw")[1])) == 5


def test_withdrawing_a_canvas_also_listed_in_config_says_to_remove_it_there(
    config, export, terminal
):
    export()
    answer(config, "all\n")
    listed_too = replace(config, slack_canvases=(CANVAS, "F00000014"))
    code, screen = answer(config, "f\n", "withdraw", settings=listed_too)
    assert code == 0
    assert "f) Slack canvas · ID F00000014" in screen and "· also listed in config.toml" in screen
    assert decisions(config)[TAB] == "withdrawn"  # still recorded
    assert "Also listed in slack_canvases in config.toml: f." in screen
    begin(listed_too, [f"example-channel={CHANNEL}"], now=LATER)
    assert "F00000014" in open_run(config.data_dir).canvases  # config.toml is its own opt-in


@pytest.mark.parametrize("typed", ["all", "b, d", "none"])
def test_withdraw_takes_all_letters_or_none(config, export, terminal, typed):
    export()
    answer(config, "all\n")
    code, _ = answer(config, typed + "\n", "withdraw")
    assert code == 0
    withdrawn = {key for key, decision in decisions(config).items() if decision == "withdrawn"}
    keys = sorted(LISTED)
    assert withdrawn == {"all": set(keys), "b, d": {keys[1], keys[3]}, "none": set()}[typed]


@pytest.mark.parametrize("typed", ["yes", "all but a", "z", "\n"])
def test_withdraw_records_nothing_for_an_answer_it_doesnt_take(config, export, terminal, typed):
    export()
    answer(config, "all\n")
    before = approvals_file(config).read_bytes()
    code, screen = answer(config, typed + "\n", "withdraw")
    if typed == "\n":
        assert code == 0 and "No answer, so nothing was recorded." in screen
    else:
        assert code == 1 and "That isn't an answer withdraw takes" in screen
    assert approvals_file(config).read_bytes() == before


def test_withdraw_with_nothing_approved_says_how_to_unlist_a_canvas(config, export, terminal):
    export()
    _, screen = answer(config, "\n", "withdraw")
    assert "Nothing was approved at review" in screen
    assert "removing it from slack_canvases there" in screen


# Where the approvals file goes


def moved(config, tmp_path, folder: Path) -> Config:
    """The same settings, with config.toml in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.toml").write_text(config.path.read_text())
    return replace(config, path=folder / "config.toml")


@pytest.mark.parametrize("step", ["review", "withdraw"])
def test_the_approvals_file_is_never_written_in_a_git_repo(
    config, export, terminal, tmp_path, capsys, step
):
    # Must refuse 12.
    export()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    in_repo = moved(config, tmp_path, tmp_path / "repo" / "settings")
    code, screen = answer(config, "all\n", step, settings=in_repo)
    assert code == 1 and screen == ""
    err = capsys.readouterr().err
    assert "inside a git repository or a folder a sync app looks after" in err
    assert "Move config.toml to the .diffdata folder in your home folder" in err
    assert not (tmp_path / "repo" / "settings" / "approvals.jsonl").exists()


def test_the_approvals_file_is_never_written_in_a_synced_folder(config, export, terminal, tmp_path):
    export()
    synced = moved(config, tmp_path, tmp_path / "OneDrive - Example" / ".diffdata")
    assert answer(config, "all\n", settings=synced)[0] == 1
    assert not (tmp_path / "OneDrive - Example" / ".diffdata" / "approvals.jsonl").exists()


def test_the_approvals_file_is_checked_after_following_a_shortcut(
    config, export, terminal, tmp_path
):
    export()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "settings").mkdir()
    shortcut = tmp_path / "home" / "linked"
    try:
        shortcut.symlink_to(tmp_path / "repo" / "settings", target_is_directory=True)
    except OSError:
        pytest.skip("this system doesn't let tests make symlinks")
    linked = moved(config, tmp_path, shortcut)
    assert answer(config, "all\n", settings=linked)[0] == 1
    assert not (tmp_path / "repo" / "settings" / "approvals.jsonl").exists()


# An approvals file diffdata can't read


DAMAGE = [
    "not JSON",
    json.dumps({"key": TAB, "decision": "maybe", "at": "2026-10-08T12:30:00Z", "answer": "all"}),
    json.dumps({"key": "url:https://example.org/", "decision": "approved", "at": "", "answer": ""}),
    json.dumps({"key": TAB, "decision": ["approved"], "at": "", "answer": ""}),
]


@pytest.mark.parametrize("damage", DAMAGE)
def test_an_approvals_file_diffdata_cant_read_is_never_written(
    config, export, terminal, capsys, damage
):
    # Must refuse 13: review and withdraw refuse, so no earlier skip or withdrawal is lost.
    export()
    answer(config, "all but e\n")
    with approvals_file(config).open("a") as f:
        f.write(damage + "\n")
    before = approvals_file(config).read_bytes()
    for step in ("review", "withdraw"):
        assert answer(config, "all\n", step) == (1, "")
        err = capsys.readouterr().err
        assert "Couldn't read the approvals file next to config.toml" in err
        assert not any(private in err for private in PRIVATE)
    assert approvals_file(config).read_bytes() == before


def test_with_an_unreadable_approvals_file_the_export_reads_config_canvases_only(
    config, export, terminal
):
    export()
    answer(config, "all\n")
    with approvals_file(config).open("a") as f:
        f.write("not JSON\n")
    report = begin(config, [f"example-channel={CHANNEL}"], now=LATER)
    assert open_run(config.data_dir).canvases == [CANVAS]
    assert "this run reads only the canvases config.toml lists" in report
    built = build(config)
    assert "Couldn't read the approvals file next to config.toml" in built
    assert "waiting for review" not in built


def test_the_latest_decision_wins(tmp_path):
    path = tmp_path / "approvals.jsonl"
    for decision in ("approved", "withdrawn", "approved"):
        approvals.record(path, [(TAB, decision)], "all", T0)
    assert approvals.read_decisions(path) == {TAB: "approved"}
    approvals.record(path, [(TAB, "withdrawn")], "f", T0)
    assert approvals.read_decisions(path) == {TAB: "withdrawn"}


# What reaches Claude, and what the screen shows


def test_the_build_says_how_many_are_waiting_with_counts_only(config, export, terminal):
    # Must refuse 14.
    report = export()
    assert "6 canvas(es) or Google file(s) are waiting for review." in report
    assert "uv run diffdata consent review" in report
    answer(config, "all but b\n")
    report = build(config)
    assert "waiting for review" not in report
    states = {key: entry["state"] for key, entry in log(config).items() if key in LISTED}
    assert states == {key: "declined" if key == SHEET else "approved" for key in LISTED}
    for private in PRIVATE:
        assert private not in report


def test_a_crafted_title_cant_pose_as_another_item(config, export, terminal):
    # Must refuse 15: line breaks, an escape sequence and a right-to-left mark in a title.
    export()
    path = config.data_dir / "references" / "references.jsonl"
    entries = list(log(config).values())
    for entry in entries:
        if entry["key"] == TEAM_NOTES:
            entry["title"] = (
                "Team notes\r\nb) Google Doc · https://docs.google.com/document/d/FAKE-OTHER-0001"
                "\x1b[2J\u202e\u2028"
            )
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    _, screen = answer(config, "\n")
    lines = items(screen)
    assert [line[:2] for line in lines] == ["a)", "b)", "c)", "d)", "e)", "f)"]
    assert lines[4].startswith(
        'e) Slack canvas · ID F00000013, with no link in the saved data · "Team notes b) '
    )
    assert "FAKE-OTHER-0001" not in "".join(lines[:4] + lines[5:])
    assert "\x1b" not in screen and "\u202e" not in screen and "\u2028" not in screen


def test_a_long_title_is_cut_short_so_it_cant_wrap_onto_a_line_of_its_own(config, export, terminal):
    # From the review: a title long enough to wrap could put "b) ..." at the start of a
    # line on screen. The link is never cut, so the real link always shows.
    export()
    path = config.data_dir / "references" / "references.jsonl"
    entries = list(log(config).values())
    for entry in entries:
        if entry["key"] == DOC:
            entry["title"] = (
                "x" * 200 + " b) Google Doc \u00b7 https://docs.google.com/FAKE-OTHER-0002"
            )
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    first = items(answer(config, "\n")[1])[0]
    assert f'"{"x" * 79}\u2026"' in first
    assert "FAKE-OTHER-0002" not in first
    assert "https://docs.google.com/document/d/FAKE-DOC-0010/edit#heading=h.fake01" in first
