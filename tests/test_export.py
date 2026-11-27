"""`diffdata export` (#128, revision 2): one command after day 7. Claude Code, the review,
Google, the scrub, the push and clean-history are all stubbed: CI never runs real Claude or
Slack. Synthetic data only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import team_store

from diffdata import cli
from diffdata.common import export
from diffdata.common.config import Config
from diffdata.consent.review import run_review as real_run_review
from diffdata.discover import slack_inbox
from diffdata.discover.slack_inbox import begin, open_run
from diffdata.store import push as store_push

CLAUDE = "/fake/bin/claude"
real_claude_command = export.claude_command


def a_config(tmp_path: Path, **changes) -> Config:
    settings = {
        "data_dir": tmp_path / "data",
        "slack_channels": ("example-channel",),
        "slack_notices": (("example-channel", "none"),),
        "slack_workspace_url": "https://example.slack.com",
    }
    return Config(**(settings | changes))


def write_run(
    data: Path, test: bool, why: str = "built", at: datetime | None = None, saved: bool = True
) -> None:
    """A closed export run, as /slack-export's build leaves it in slack/runs/, with one result
    the hook saved in the inbox unless `saved` is False."""
    at = at or datetime.now(UTC)
    run_id = f"{at:%Y%m%dT%H%M%S}{'-test' if test else ''}Z"
    run = {
        "run_id": run_id,
        "started_at": slack_inbox.utc_iso(at),
        "closed_at": slack_inbox.utc_iso(at + timedelta(seconds=1)),
        "closed_why": why,
        "test": test,
    }
    folder = slack_inbox.slack_dir(data) / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{run_id}.json").write_text(json.dumps(run), encoding="utf-8")
    if saved:
        inbox = slack_inbox.inbox_dir(data) / "C0FAKE001" / run_id
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "slack_read_channel-fake.json").write_text("{}")


def write_push(data: Path, at: datetime, complete: bool = True) -> None:
    folder = data / store_push.LOCAL_PUSHES
    folder.mkdir(parents=True, exist_ok=True)
    record = {"pushed_at": slack_inbox.utc_iso(at), "complete": complete}
    (folder / f"{at:%Y-%m-%dT%H-%M-%SZ}-fake.json").write_text(json.dumps(record))


class World:
    """Everything `diffdata export` calls, faked. Tests change the fields."""

    def __init__(self, config: Config, answers: list[str]):
        self.config = config
        self.data = config.data_dir
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []
        self.calls: list[str] = []  # each step, in order
        self.claude_argv: list[list[str]] = []
        self.builds_test = True  # whether a test run's build finishes
        self.builds_export = True
        self.hook_saves = True  # whether the save hook saved anything in Claude's run
        self.approved: set[str] = set()  # canvases approved at review
        self.approve_at_review: list[set[str]] = []  # approved by each review, in turn
        self.read: set[str] = set()  # canvases with saved reads
        self.docs: list[str] = []
        self.fetched: set[str] = set()
        self.complete = True
        self.pushed_with: list[str | None] = []

    def ask(self, question: str) -> str:
        self.asked.append(question)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.said.append(str(text))

    def claude(self, argv: list[str]) -> None:
        self.claude_argv.append(argv)
        test = argv[-1].endswith("test")
        self.calls.append("claude test" if test else "claude")
        if (self.builds_test if test else self.builds_export) and argv[-1].startswith("/"):
            write_run(self.data, test, saved=self.hook_saves)
            if not test:
                self.read |= self.approved

    def review(self, config, *rest) -> int:
        assert not rest, "the review gets no answer and no streams from export"
        self.calls.append("review")
        if self.approve_at_review:
            self.approved |= self.approve_at_review.pop(0)
        return 0

    def scrub(self, config) -> str:
        self.calls.append("scrub")
        folder = self.data / "scrubbed"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(f'{{"scrub": {len(self.calls)}}}')
        return "Scrubbed 2 copies."

    def push(self, config, answered=None):
        self.calls.append("push")
        self.pushed_with.append(answered)
        report = store_push.Report("2 scrubbed copy(ies) added to the store.")
        report.complete = self.complete
        return report

    def clean(self, ask, say, data=None) -> int:
        self.calls.append("clean-history")
        return 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    def make(answers=(), **changes) -> World:
        w = World(a_config(tmp_path, **changes), list(answers))
        w.data.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(export, "by_a_person", lambda: True)
        monkeypatch.setattr(export, "load_config", lambda: w.config)
        monkeypatch.setattr(export.check, "state_items", lambda: [])
        monkeypatch.setattr(export, "claude_command", lambda: [CLAUDE])
        monkeypatch.setattr(export.review, "run_review", w.review)
        monkeypatch.setattr(export.approvals, "approved_canvases", lambda c: sorted(w.approved))
        monkeypatch.setattr(export.references, "canvases_read", lambda d: set(w.read))
        monkeypatch.setattr(
            export.google_fetch, "to_fetch", lambda c: ([(d, "Entry") for d in w.docs], True)
        )
        monkeypatch.setattr(
            export.references,
            "google_docs_fetched",
            lambda d: ({f"google:{x}" for x in w.fetched}, {}),
        )

        def sign_in(listed=()):
            w.calls.append("google sign-in")
            return "Signed in."

        def collect(config):
            w.calls.append("google fetch")
            w.fetched |= set(w.docs)
            return "Fetched."

        monkeypatch.setattr(export.google_signin, "sign_in", sign_in)
        monkeypatch.setattr(export.google_fetch, "collect", collect)
        monkeypatch.setattr(export.scrub_run, "saved_left_out", lambda c: ({}, {}, "stamp"))
        monkeypatch.setattr(export.slack_inbox, "applied_stamp", lambda d: "stamp")
        monkeypatch.setattr(export.scrub_run, "scrub", w.scrub)
        monkeypatch.setattr(export.store_push, "push", w.push)
        monkeypatch.setattr(export.history, "clean", w.clean)
        return w

    return make


def run(w: World) -> int:
    return export.run(ask=w.ask, say=w.say, claude=w.claude)


# The whole run


def test_one_command_runs_every_step_in_order(world):
    w = world(["y", "push"])
    assert run(w) == 0
    assert w.calls == [
        "claude test",
        "claude",
        "review",
        "scrub",
        "push",
        "clean-history",
    ]
    assert [argv[-1] for argv in w.claude_argv] == ["/slack-export test", "/slack-export"]
    # Before each start: how to come back, and which account to sign in with.
    assert sum("type /exit" in s and "same Claude account" in s for s in w.said) == 2


def test_the_review_starts_by_itself_with_no_question_first(world):
    w = world(["y", "push"])
    run(w)
    review_at = w.calls.index("review")
    # Two questions only: the export, then the push. None before the review.
    assert len(w.asked) == 2 and "Export now?" in w.asked[0]
    assert w.calls[review_at - 1] == "claude"


def test_a_failed_test_run_stops_before_the_export(world):
    w = world(["y", "push"])
    w.builds_test = False
    assert run(w) == 1
    assert w.calls == ["claude test"]
    assert w.asked == []
    assert any("The test run didn't finish" in s for s in w.said)


def test_no_to_export_now_stops_with_nothing_more_read(world):
    w = world(["n"])
    assert run(w) == 0
    assert w.calls == ["claude test"]


def test_a_failed_export_stops_before_the_review(world):
    w = world(["y"])
    w.builds_export = False
    assert run(w) == 1
    assert w.calls == ["claude test", "claude"]


def test_an_earlier_test_runs_build_doesnt_count_as_this_ones(world):
    w = world(["y"])
    write_run(w.data, test=True, at=datetime.now(UTC) - timedelta(hours=1))
    w.builds_test = False
    assert run(w) == 1
    assert w.calls == ["claude test"]


# Rounds


def test_approved_canvases_get_another_export_then_another_review(world):
    w = world(["y", "", "push"])
    w.approve_at_review = [{"F0FAKE001"}]
    assert run(w) == 0
    assert w.calls == [
        "claude test",
        "claude",
        "review",
        "claude",
        "review",
        "scrub",
        "push",
        "clean-history",
    ]


def test_google_docs_are_signed_in_and_fetched_once_then_reviewed(world):
    w = world(["y", "", "push"])
    w.docs = ["FAKE-doc-id-0000000000001"]
    assert run(w) == 0
    assert w.calls[2:6] == ["review", "google sign-in", "google fetch", "review"]
    assert w.calls.count("google fetch") == 1


def test_docs_fetched_before_skip_the_sign_in(world):
    w = world(["y", "", "push"])
    w.docs = ["FAKE-doc-id-0000000000001"]
    w.fetched = set(w.docs)
    run(w)
    assert "google sign-in" not in w.calls and "google fetch" in w.calls


def test_stop_in_the_rounds_stops_before_the_scrub(world):
    w = world(["y", "stop"])
    w.approve_at_review = [{"F0FAKE001"}]
    assert run(w) == 1
    assert "scrub" not in w.calls and "push" not in w.calls
    assert any("review answers so far are kept" in s for s in w.said)


def test_the_rounds_stop_after_three(world):
    w = world(["y", "", "", "", "push"])
    w.approve_at_review = [{"F0FAKE001"}, {"F0FAKE002"}, {"F0FAKE003"}, {"F0FAKE004"}]
    assert run(w) == 0
    assert w.calls.count("review") == 4
    assert w.calls.count("claude") == 4  # the export, then three rounds
    assert any("That's 3 rounds" in s for s in w.said)


def test_a_canvas_that_stays_unread_isnt_tried_again(world):
    w = world(["y", "", "push"])
    w.approve_at_review = [{"F0FAKE001"}]
    w.builds_export = True
    original = w.claude

    def claude_that_cant_read_it(argv):
        original(argv)
        w.read.discard("F0FAKE001")

    w.claude = claude_that_cant_read_it
    assert run(w) == 0
    assert w.calls.count("claude") == 2


# Scrub


def test_a_changed_opt_out_list_rebuilds_before_the_scrub(world, monkeypatch):
    w = world(["y", "push"])
    monkeypatch.setattr(export.slack_inbox, "applied_stamp", lambda d: "older")
    monkeypatch.setattr(export.slack_export, "build", lambda c: w.calls.append("build") or "Built.")
    run(w)
    assert w.calls[w.calls.index("scrub") - 1] == "build"


# Resume


def test_run_again_after_an_export_starts_at_the_review(world):
    w = world(["push"])
    write_run(w.data, test=False, at=datetime.now(UTC) - timedelta(hours=1))
    assert run(w) == 0
    assert w.calls == ["review", "scrub", "push", "clean-history"]
    assert w.claude_argv == []


def test_run_again_after_a_complete_push_starts_a_new_export(world):
    w = world(["y", "push"])
    write_run(w.data, test=False, at=datetime.now(UTC) - timedelta(hours=2))
    write_push(w.data, datetime.now(UTC) - timedelta(hours=1))
    run(w)
    assert w.calls[:2] == ["claude test", "claude"]


def test_a_push_that_stopped_partway_doesnt_end_the_export(world):
    w = world(["push"])
    write_run(w.data, test=False, at=datetime.now(UTC) - timedelta(hours=2))
    write_push(w.data, datetime.now(UTC) - timedelta(hours=1), complete=False)
    run(w)
    assert w.calls[0] == "review"


def test_only_a_test_run_since_the_push_starts_from_the_test(world):
    w = world(["y", "push"])
    write_run(w.data, test=True, at=datetime.now(UTC) - timedelta(hours=1))
    run(w)
    assert w.calls[0] == "claude test"


# Must-refuse 1: only a person, in their own terminal, and never under Claude Code


def test_refuses_outside_a_terminal_before_anything(world, monkeypatch):
    w = world(["y", "push"])
    monkeypatch.setattr(export, "by_a_person", lambda: False)
    monkeypatch.setattr(export, "load_config", lambda: pytest.fail("config.toml was read"))
    assert run(w) == 1
    assert w.calls == [] and w.asked == []
    assert "your own terminal" in w.said[0]


def test_refuses_with_claudecode_set_even_in_a_terminal(monkeypatch):
    monkeypatch.setattr(export.review, "in_terminal", lambda *streams: True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert export.by_a_person() is False
    monkeypatch.delenv("CLAUDECODE")
    assert export.by_a_person() is True


def test_refuses_when_input_or_output_isnt_a_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(export.review, "in_terminal", lambda *streams: False)
    assert export.by_a_person() is False


def test_the_cli_runs_the_same_refusal(monkeypatch, capsys):
    monkeypatch.setattr(export.review, "in_terminal", lambda *streams: False)
    assert cli.main(["export"]) == 1
    assert "your own terminal" in capsys.readouterr().out


# Must-refuse 2: the review's own code, and no way to answer it for the person


def test_export_takes_no_options(capsys):
    for flag in ("--yes", "-y", "--push"):
        with pytest.raises(SystemExit):
            cli.main(["export", flag])


def test_the_review_is_the_reviews_own_code_with_its_own_terminal_check(world, monkeypatch):
    w = world(["y", "n", "push"])  # n: don't try the review again
    seen = []
    monkeypatch.setattr(
        export.review, "run_review", lambda c, *r: seen.append(r) or real_run_review(c)
    )
    monkeypatch.setattr(export.review, "in_terminal", lambda *streams: False)
    assert run(w) == 1  # review refuses outside a terminal, and export stops on it
    assert seen == [()]
    assert "scrub" not in w.calls


# Must-refuse 3: a push typed for this scrub only


def test_no_push_without_the_typed_word(world):
    for answer in ("", "y", "yes", "Push it"):
        w = world(["y", answer])
        assert run(w) == 0
        assert "push" not in w.calls and "clean-history" not in w.calls
        assert any("Nothing was sent" in s for s in w.said)


def test_push_is_for_the_manifest_the_producer_read(world):
    w = world(["y", "push"])
    run(w)
    manifest = w.data / "scrubbed" / "manifest.json"
    assert w.pushed_with == [store_push.manifest_digest(w.data)]
    assert w.pushed_with[0] is not None and manifest.exists()


def test_a_push_typed_on_an_earlier_run_doesnt_count(world):
    w = world(["y", "push"])
    run(w)
    w.answers, w.calls = [""], []  # run again: the export is done, so it goes to the review
    assert run(w) == 0
    assert "push" not in w.calls


# Must-refuse 4: Claude gets the slash command and nothing else


def test_claude_gets_only_the_slash_command(world):
    w = world(["y", "", "push"])
    w.approve_at_review = [{"F0FAKE001"}]
    run(w)
    for argv in w.claude_argv:
        assert argv[:-1] == [CLAUDE]
        assert argv[-1] in ("/slack-export test", "/slack-export")


def test_claude_starts_in_the_repo_folder_with_no_flags(monkeypatch):
    started = []
    monkeypatch.setattr(export.subprocess, "run", lambda argv, **kw: started.append((argv, kw)))
    export.start_claude([CLAUDE, "/slack-export"])
    argv, kw = started[0]
    assert argv == [CLAUDE, "/slack-export"]
    env = kw.pop("env")
    assert kw == {"cwd": export.REPO, "check": False}
    assert not any(a.startswith("-") for a in argv)
    # #159: only the child's environment says it's under the export; the rest is passed on.
    assert env == {**export.os.environ, "DIFFDATA_EXPORT": "1"}
    assert "DIFFDATA_EXPORT" not in export.os.environ


def test_after_claude_closes_the_export_says_where_it_is_and_what_comes_next(world):
    w = world(["y", "push"])
    run(w)
    back = [s for s in w.said if s.startswith("\nBack in diffdata export.")]
    assert back == [
        "\nBack in diffdata export. The test run is done. Next: it asks whether to export now.",
        "\nBack in diffdata export. The Slack export is done. Next: the review, which opens "
        "here by itself.",
    ]


def test_no_permission_flag_anywhere_in_the_code():
    text = Path(export.__file__).read_text(encoding="utf-8")
    for flag in ("--dangerously", "--permission", "--allowed", "--allowedTools", "bypass"):
        assert flag not in text


def test_claude_is_found_on_path_or_in_the_desktop_app(tmp_path, monkeypatch):
    monkeypatch.setattr(export.check, "claude_path", lambda: None)
    version = tmp_path / "2.1.331"
    binary = version / "fakehash" / "claude.app" / "Contents" / "MacOS" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(export.check, "desktop_scan", lambda: ({(2, 1, 331): version}, False))
    assert export.claude_command() == [str(binary)]
    monkeypatch.setattr(export.check, "claude_path", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(export.check, "desktop_scan", lambda: pytest.fail("looked in the app"))
    assert export.claude_command() == ["/usr/local/bin/claude"]


def test_a_blocked_desktop_app_folder_says_how_to_go_on_not_missing(world, monkeypatch):
    # #160: macOS refused the Claude app's folder: it's not "Claude Code wasn't found".
    w = world(["y", "push"])
    monkeypatch.setattr(export, "claude_command", real_claude_command)
    monkeypatch.setattr(export.check, "claude_path", lambda: None)
    monkeypatch.setattr(export.check, "desktop_scan", lambda: ({}, True))
    assert run(w) == 1
    said = "\n".join(w.said)
    assert "macOS blocked Terminal" in said and "Privacy & Security" in said
    assert "`claude` command" in said and "wasn't found" not in said
    assert w.calls == []


def test_no_claude_stops_with_the_line_to_type(world, monkeypatch):
    w = world(["y", "push"])
    monkeypatch.setattr(export, "claude_command", lambda: None)
    assert run(w) == 1
    assert w.calls == []
    assert any("/slack-export test" in s for s in w.said)


# Must-refuse 5: nothing starts before day 7 or without the opt-out list


def test_refuses_before_day_7_without_starting_claude(world):
    posted = int((datetime.now(UTC) - timedelta(days=3)).timestamp())
    w = world(["y", "push"], slack_notices=(("example-channel", f"C0FAKE001/{posted}.000000"),))
    assert run(w) == 1
    assert w.calls == []
    assert any("opt-out window is still open" in s for s in w.said)


def test_refuses_without_the_teams_opt_out_list(world, team_lists, tmp_path):
    team_lists.lists = {}  # the store has no list for the team
    w = world(["y", "push"], **team_store(tmp_path))
    assert run(w) == 1
    assert w.calls == []
    assert any("opt-out list isn't in the store yet" in s for s in w.said)


def test_setup_not_done_stops_before_claude(world, monkeypatch):
    w = world(["y", "push"])
    gap = export.check.Item("config.toml", False, "Run uv run diffdata setup.")
    monkeypatch.setattr(export.check, "state_items", lambda: [gap])
    assert run(w) == 1
    assert w.calls == []


# Must-refuse 6: clean-history after Claude has ended, and only after a full push


def test_clean_history_runs_last_after_every_claude_has_ended(world):
    w = world(["y", "", "push"])
    w.approve_at_review = [{"F0FAKE001"}]
    run(w)
    assert w.calls[-1] == "clean-history"
    assert w.calls.index("clean-history") > max(i for i, c in enumerate(w.calls) if "claude" in c)


def test_no_clean_history_after_a_push_that_didnt_send_everything(world):
    w = world(["y", "push"])
    w.complete = False
    run(w)
    assert "clean-history" not in w.calls
    assert any("clean-history" in s for s in w.said)  # how to run it later


# Must-refuse 8: nothing private printed


def test_an_unexpected_error_shows_only_its_type(world, monkeypatch):
    w = world(["y", "push"])

    def boom(config):
        raise ValueError("F0FAKE001 secret canvas title")

    monkeypatch.setattr(export.scrub_run, "scrub", boom)
    assert run(w) == 1
    assert not any("F0FAKE001" in s for s in w.said)
    assert any("unexpected ValueError" in s for s in w.said)


def test_closed_input_says_how_to_carry_on(world):
    w = world(["y"])  # then the push question finds no more answers: closed input
    assert run(w) == 130
    assert "review answers so far are kept" in w.said[-1]


def test_ctrl_c_stops_and_says_how_to_carry_on(world, monkeypatch):
    w = world(["y", "push"])

    def interrupted(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(export.scrub_run, "scrub", interrupted)
    assert run(w) == 130
    assert "push" not in w.calls
    assert "review answers so far are kept" in w.said[-1]


# From Codex's review


def test_config_is_read_again_after_the_list_fetch_before_the_scrub_and_before_the_push(
    world, monkeypatch
):
    # Each read gives a new snapshot, so a stale one shows up as an old number.
    w = world(["y", "push"])
    loads = []

    def load_config():
        loads.append(a_config(w.data.parent, slack_opted_out=(f"U0FAKE{len(loads):03}",)))
        return loads[-1]

    seen = {}
    monkeypatch.setattr(export, "load_config", load_config)
    monkeypatch.setattr(
        export.team_optouts, "fetch_list", lambda c, closes: seen.setdefault("fetch", c) and ""
    )
    monkeypatch.setattr(
        export.scrub_run, "saved_left_out", lambda c: seen.setdefault("scrub", c) and ({}, {}, "")
    )
    monkeypatch.setattr(export.slack_inbox, "applied_stamp", lambda d: "")
    monkeypatch.setattr(
        export.store_push, "push", lambda c, answered=None: seen.setdefault("push", c) and ""
    )
    run(w)
    assert seen["fetch"] is loads[0]
    order = [loads.index(seen[step]) for step in ("fetch", "scrub", "push")]
    assert order == sorted(order) and len(set(order)) == 3
    assert seen["push"] is loads[-1]


def test_a_build_with_nothing_saved_isnt_a_test_that_worked(world):
    w = world(["y", "push"])
    w.hook_saves = False
    assert run(w) == 1
    assert w.calls == ["claude test"] and w.asked == []


def test_the_checks_run_again_before_each_claude(world, monkeypatch):
    # A channel with a fresh notice added while "Export now?" waited stops the export.
    w = world(["y", "push"])
    posted = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    fresh = a_config(w.data.parent, slack_notices=(("example-channel", f"C0FAKE001/{posted}.0"),))
    original = w.claude

    def claude_then_a_new_notice(argv):
        original(argv)
        w.config = fresh

    w.claude = claude_then_a_new_notice
    assert run(w) == 1
    assert w.calls == ["claude test"]
    assert any("opt-out window is still open" in s for s in w.said)


def test_two_channels_listed_by_id_are_both_checked_for_day_7(world):
    posted = int((datetime.now(UTC) - timedelta(days=3)).timestamp())
    w = world(
        ["y", "push"],
        slack_channels=(),
        slack_channel_ids=("C0FAKE001", "C0FAKE002"),
        slack_notices=(("C0FAKE001", f"C0FAKE001/{posted}.000000"), ("C0FAKE002", "none")),
    )
    assert run(w) == 1
    assert w.calls == []
    assert not any("C0FAKE001" in s for s in w.said)


def test_typed_ahead_input_is_dropped_before_the_review_and_clean_history(world, monkeypatch):
    w = world(["y", "push"])
    monkeypatch.setattr(export, "drop_typed_ahead", lambda: w.calls.append("drop"))
    run(w)
    assert w.calls[w.calls.index("review") - 1] == "drop"
    assert w.calls[w.calls.index("clean-history") - 1] == "drop"


def test_a_push_whose_record_wasnt_sent_doesnt_end_the_export(world):
    w = world(["push"])
    write_run(w.data, test=False, at=datetime.now(UTC) - timedelta(hours=2))
    folder = w.data / store_push.LOCAL_PUSHES
    folder.mkdir(parents=True)
    at = slack_inbox.utc_iso(datetime.now(UTC) - timedelta(hours=1))
    record = {"pushed_at": at, "complete": True, "record_sent": False}
    (folder / "fake.json").write_text(json.dumps(record))
    run(w)
    assert w.calls[0] == "review"


def test_a_run_from_before_the_test_flag_doesnt_count_as_an_export(world):
    w = world(["y", "push"])
    folder = slack_inbox.slack_dir(w.data) / "runs"
    folder.mkdir(parents=True)
    old = {"started_at": "2026-01-01T10:00:00Z", "closed_at": "2026-01-01T10:05:00Z"}
    (folder / "old.json").write_text(json.dumps(old | {"closed_why": "built"}))
    run(w)
    assert w.calls[0] == "claude test"


def test_claude_exe_right_in_the_version_folder_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(export.check, "claude_path", lambda: None)
    version = tmp_path / "2.1.331"
    version.mkdir()
    (version / "claude.exe").write_text("")
    monkeypatch.setattr(export.check, "desktop_scan", lambda: ({(2, 1, 331): version}, False))
    assert export.claude_command() == [str(version / "claude.exe")]


def test_a_lost_google_sign_in_signs_in_again_once(world, monkeypatch):
    w = world(["y", "", "push"])
    w.docs = ["FAKE-doc-id-0000000000001"]
    w.fetched = set(w.docs)  # fetched before, so no sign-in first
    tries = []

    def collect(config):
        tries.append(1)
        w.calls.append("google fetch")
        if len(tries) == 1:
            raise export.google_signin.SignInError("Not signed in to Google yet.")
        return "Fetched."

    monkeypatch.setattr(export.google_fetch, "collect", collect)
    assert run(w) == 0
    assert w.calls[3:6] == ["google fetch", "google sign-in", "google fetch"]


# The run's test flag, which tells a test run from an export


def test_begin_marks_a_test_run(tmp_path):
    config = a_config(tmp_path)
    when = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    begin(config, ["example-channel=C00000001"], now=when, test=True)
    assert open_run(config.data_dir).test is True
    begin(config, ["example-channel=C00000001"], now=when + timedelta(minutes=1))
    assert open_run(config.data_dir).test is False


def test_the_slack_export_command_marks_its_test_run():
    body = (Path(export.REPO) / ".claude" / "commands" / "slack-export.md").read_text()
    assert "add `--test` to `begin` in step 2" in body
