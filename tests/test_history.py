"""`diffdata clean-history` and push's offer (#150, item 7). One test or more per must-refuse
item 4 to 10.

Everything runs in a fake home with a made-up Claude folder: the real ~/.claude is never read.
The repo, the sibling project and every path are made up.
"""

import json
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from diffdata import cli
from diffdata.common import history

REAL_BY_A_PERSON = history.by_a_person  # before the autouse fixture stands in for it


@pytest.fixture(autouse=True)
def a_person(monkeypatch):
    """clean() itself refuses unless a person in a terminal runs it, and pytest has no
    terminal: tests stand in for the person. The tests of that refusal undo this."""
    monkeypatch.setattr(history, "by_a_person", lambda: True)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake home with a Claude folder, a made-up repo, and its project folders."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(history.CLAUDE_MARK, raising=False)
    claude = home / ".claude"
    projects = claude / "projects"
    projects.mkdir(parents=True)
    (claude / "settings.json").write_text('{"model": "x"}', encoding="utf-8")
    (claude / "history.jsonl").write_text('{"display": "typed"}\n', encoding="utf-8")
    repo = home / "code" / "differential-data-collection"
    (repo / ".claude" / "worktrees" / "wt").mkdir(parents=True)
    diffdata = home / ".diffdata"
    diffdata.mkdir()
    (diffdata / "config.toml").write_text("# made up\n", encoding="utf-8")
    data = home / "diffdata-data"
    data.mkdir()
    (data / "kept.md").write_text("made-up data\n", encoding="utf-8")
    return {"home": home, "claude": claude, "projects": projects, "repo": repo, "data": data}


def session(folder: Path, cwd: str | None, name: str = "s1", lines: int = 1) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    entries = [{"type": "summary"}] * (lines - 1)
    entries.append({"type": "user", **({"cwd": cwd} if cwd is not None else {})})
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def project(fake, path: Path) -> Path:
    return fake["projects"] / history.encode(path)


def snapshot(*roots: Path) -> dict[str, bytes]:
    found = {}
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                found[str(path)] = path.read_bytes()
    return found


class Answers:
    """Answers in order. `events` holds what was said and asked, in the order it happened."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.events: list[str] = []

    def ask(self, question: str) -> str:
        self.events.append(f"ASK {question}")
        assert self.answers, f"asked more than the test answered: {question!r}"
        return self.answers.pop(0)

    def say(self, line: str) -> None:
        self.events.append(line)

    @property
    def said(self) -> list[str]:
        return [e for e in self.events if not e.startswith("ASK ")]

    @property
    def asked(self) -> list[str]:
        return [e[4:] for e in self.events if e.startswith("ASK ")]

    @property
    def text(self) -> str:
        return "\n".join(self.events)


def clean(fake, *answers: str) -> tuple[int, Answers]:
    script = Answers(*answers)
    code = history.clean(script.ask, script.say, repo=fake["repo"], data=fake["data"])
    return code, script


@pytest.fixture
def layout(fake):
    """This repo's folder, a worktree's folder, a sibling project whose name starts like this
    repo's, a matching folder with a session with no cwd, and an unrelated project."""
    repo = fake["repo"]
    ours = project(fake, repo)
    session(ours, str(repo), "a")
    session(ours, str(repo / "src"), "b", lines=5)
    (ours / "a").mkdir()
    (ours / "a" / "tool-results").mkdir()
    (ours / "a" / "tool-results" / "out.txt").write_text("made up", encoding="utf-8")
    worktree = project(fake, repo / ".claude" / "worktrees" / "wt")
    session(worktree, str(repo / ".claude" / "worktrees" / "wt"))
    sibling_path = repo.parent / "differential-data-collection-old"
    sibling = project(fake, sibling_path)
    session(sibling, str(sibling_path))
    no_cwd = fake["projects"] / (history.encode(repo) + "-x")
    session(no_cwd, None)
    other = fake["projects"] / history.encode(fake["home"] / "elsewhere")
    session(other, str(fake["home"] / "elsewhere"))
    return {
        "ours": ours,
        "worktree": worktree,
        "sibling": sibling,
        "no_cwd": no_cwd,
        "other": other,
    }


# --- must refuse 4: a person in a terminal, not Claude -----------------------------------------


def test_must_refuse_4_outside_a_terminal_nothing_is_deleted(fake, layout, monkeypatch, capsys):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: False)
    monkeypatch.setattr(history, "REPO", fake["repo"])
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    before = snapshot(fake["home"])
    assert cli.main(["clean-history"]) == 1
    assert "your own terminal" in capsys.readouterr().err
    assert snapshot(fake["home"]) == before


def test_must_refuse_4_a_claude_code_shell_is_refused(fake, layout, monkeypatch, capsys):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setenv(history.CLAUDE_MARK, "1")
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    before = snapshot(fake["home"])
    assert cli.main(["clean-history"]) == 1
    assert "never Claude" in capsys.readouterr().err
    assert snapshot(fake["home"]) == before


def test_must_refuse_4_the_repo_settings_deny_it_to_claudes_shells():
    settings = json.loads(
        (Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text(
            encoding="utf-8"
        )
    )
    deny = settings["permissions"]["deny"]
    for tool in ("Bash", "PowerShell"):
        for pattern in (
            "*diffdata clean-history*",
            "*diffdata.exe clean-history*",
            "*common.history*",
            "*common import history*",
        ):
            assert f"{tool}({pattern})" in deny


# --- must refuse 5: shown first, then the typed word yes ---------------------------------------


@pytest.mark.parametrize("answers", [("", "y", "no"), ("YES please", "no"), ("no",)])
def test_must_refuse_5_anything_but_yes_deletes_nothing(fake, layout, answers):
    before = snapshot(fake["home"])
    code, script = clean(fake, *answers)
    assert code == 0
    assert snapshot(fake["home"]) == before
    assert history.NOTHING_DELETED in script.text
    assert script.asked == [history.ASK] * len(answers)  # asked again until no


def test_must_refuse_5_the_list_comes_before_the_question(fake, layout):
    code, script = clean(fake, "yes")
    assert code == 0
    events = script.events
    question = events.index(f"ASK {history.ASK}")
    assert events.index(f"  {layout['ours'].name}: 2 session(s)") < question
    assert events.index(f"  {layout['worktree'].name}: 1 session(s)") < question
    assert script.asked == [history.ASK]


def test_yes_deletes_only_the_listed_folders(fake, layout):
    gone = (layout["ours"], layout["worktree"])
    keep = {k: v for k, v in snapshot(fake["home"]).items() if not set(gone) & set(Path(k).parents)}
    code, script = clean(fake, "yes")
    assert code == 0
    assert not layout["ours"].exists() and not layout["worktree"].exists()
    assert snapshot(fake["home"]) == keep
    assert "Deleted 2 of 2 folder(s)" in script.text


# --- must refuse 6: never another project's sessions -------------------------------------------


def test_must_refuse_6_other_projects_are_left_alone_and_named(fake, layout):
    clean(fake, "yes")
    for name in ("sibling", "no_cwd", "other"):
        assert layout[name].exists(), name
    _, script = clean(fake, "no")
    assert f"Left alone: {layout['sibling'].name} (a session from another folder)." in script.said
    assert f"Left alone: {layout['no_cwd'].name} (a session with no recorded folder)." in (
        script.said
    )
    assert layout["other"].name not in script.text  # its name doesn't even match


def test_must_refuse_6_one_foreign_session_keeps_the_whole_folder(fake, layout):
    session(layout["ours"], str(fake["home"] / "elsewhere"), "c")
    clean(fake, "yes")
    assert layout["ours"].exists()
    assert not layout["worktree"].exists()


def test_must_refuse_6_a_folder_with_no_sessions_is_left_alone(fake):
    empty = project(fake, fake["repo"])
    empty.mkdir()
    (empty / "notes.txt").write_text("x", encoding="utf-8")
    code, script = clean(fake)
    assert code == 0
    assert empty.exists()
    assert "no session files to check" in script.text


def test_a_cwd_is_compared_as_a_path_not_as_text(fake):
    repo = fake["repo"]
    folder = project(fake, repo)
    session(folder, str(repo) + "-old")  # starts with the repo's path as text
    clean(fake)
    assert folder.exists()


def test_the_cwd_is_read_from_the_first_lines_only(fake):
    folder = project(fake, fake["repo"])
    session(folder, str(fake["repo"]), lines=history.CWD_LINES + 5)
    _, script = clean(fake)
    assert folder.exists()
    assert "no recorded folder" in script.text


# --- must refuse 7: nothing outside the matched folders ----------------------------------------


def test_must_refuse_7_settings_history_data_and_diffdata_are_untouched(fake, layout):
    outside = {
        k: v
        for k, v in snapshot(fake["home"]).items()
        if str(fake["projects"]) not in k or str(layout["other"]) in k
    }
    clean(fake, "yes")
    after = snapshot(fake["home"])
    for path, data in outside.items():
        assert after.get(path) == data, path
    assert fake["projects"].is_dir()


def test_must_refuse_7_a_link_planted_in_projects_is_never_followed(fake):
    target = project(fake, fake["repo"])
    try:
        target.symlink_to(fake["data"], target_is_directory=True)
    except OSError:
        pytest.skip("this system won't make links")
    before = snapshot(fake["data"])
    _, script = clean(fake)
    assert snapshot(fake["data"]) == before
    assert "it's a link" in script.text


def test_must_refuse_7_a_link_inside_a_matched_folder_keeps_it(fake):
    folder = project(fake, fake["repo"])
    session(folder, str(fake["repo"]))
    try:
        (folder / "escape").symlink_to(fake["data"], target_is_directory=True)
    except OSError:
        pytest.skip("this system won't make links")
    before = snapshot(fake["data"])
    clean(fake)
    assert folder.exists()
    assert snapshot(fake["data"]) == before


def test_must_refuse_7_a_folder_holding_the_data_folder_is_left_alone(fake):
    folder = project(fake, fake["repo"])
    session(folder, str(fake["repo"]))
    inside = folder / "data"
    inside.mkdir()
    script = Answers()
    history.clean(script.ask, script.say, repo=fake["repo"], data=inside)
    assert folder.exists()
    assert "overlaps" in script.text  # the whole run stops: the data folder is under projects/


def test_must_refuse_7_a_change_after_the_list_is_caught(fake, layout):
    script = Answers()

    def ask_then_change(question):
        script.asked.append(question)
        session(layout["ours"], str(fake["home"] / "elsewhere"), "late")  # appears meanwhile
        return "yes"

    history.clean(ask_then_change, script.say, repo=fake["repo"], data=fake["data"])
    assert layout["ours"].exists()
    assert not layout["worktree"].exists()
    assert "it changed while you read the list" in script.text


def test_claude_config_dir_is_respected(fake, monkeypatch, tmp_path):
    other = tmp_path / "custom-claude"
    folder = other / "projects" / history.encode(fake["repo"])
    session(folder, str(fake["repo"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    clean(fake, "yes")
    assert not folder.exists()


# --- must refuse 8: nothing matches ------------------------------------------------------------


def test_must_refuse_8_no_match_deletes_nothing_and_exits_0(fake):
    before = snapshot(fake["home"])
    code, script = clean(fake)
    assert code == 0
    assert script.asked == []
    assert "No Claude Code session history for this repo" in script.text
    assert snapshot(fake["home"]) == before


# --- must refuse 9 and 10: push's offer --------------------------------------------------------


@pytest.fixture
def pushed(fake, layout, monkeypatch):
    monkeypatch.setattr(history, "REPO", fake["repo"])
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(data_dir=fake["data"]))
    monkeypatch.setattr(cli.store_push, "push", lambda config: report(complete=True))
    return layout


def report(complete: bool):
    done = cli.store_push.Report("1 scrubbed copy(ies) added.")
    done.complete = complete
    return done


def test_must_refuse_9_a_push_that_stopped_offers_nothing(fake, pushed, monkeypatch, capsys):
    def stopped(config):
        raise cli.store_push.PushError("made-up stop")

    monkeypatch.setattr(cli.store_push, "push", stopped)
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setattr(history, "clean", lambda *a, **k: pytest.fail("offered"))
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    assert cli.main(["push"]) == 1
    assert history.OFFER not in capsys.readouterr().out


@pytest.mark.parametrize("terminal, claude", [(False, False), (True, True)])
def test_must_refuse_9_no_offer_outside_a_terminal_or_in_claude(
    fake, pushed, monkeypatch, capsys, terminal, claude
):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: terminal)
    if claude:
        monkeypatch.setenv(history.CLAUDE_MARK, "1")
    monkeypatch.setattr(history, "clean", lambda *a, **k: pytest.fail("offered"))
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    assert cli.main(["push"]) == 0
    out = capsys.readouterr().out
    assert history.LATER in out and history.OFFER not in out


@pytest.mark.parametrize("answers", [[""], ["no"], ["y", ""], ["yes", "no"], ["yes", "y", "no"]])
def test_must_refuse_10_the_offer_deletes_nothing_without_yes(
    fake, pushed, monkeypatch, capsys, answers
):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda q="": queue.pop(0))
    before = snapshot(fake["home"])
    assert cli.main(["push"]) == 0
    assert snapshot(fake["home"]) == before
    assert history.OFFER in capsys.readouterr().out


def test_the_offer_with_two_yeses_deletes_this_repos_history(fake, pushed, monkeypatch):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    queue = ["yes", "yes"]
    monkeypatch.setattr("builtins.input", lambda q="": queue.pop(0))
    assert cli.main(["push"]) == 0
    assert not pushed["ours"].exists()
    assert pushed["sibling"].exists()


def test_the_command_in_a_terminal_cleans(fake, layout, monkeypatch):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setattr(history, "REPO", fake["repo"])
    monkeypatch.setattr(cli, "history_data_dir", lambda: fake["data"])
    monkeypatch.setattr("builtins.input", lambda q="": "yes")
    assert cli.main(["clean-history"]) == 0
    assert not layout["ours"].exists()
    assert layout["sibling"].exists()


def test_must_refuse_9_a_push_that_left_a_passed_copy_unsent_offers_nothing(
    fake, pushed, monkeypatch, capsys
):
    monkeypatch.setattr(cli.store_push, "push", lambda config: report(complete=False))
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    assert cli.main(["push"]) == 0
    out = capsys.readouterr().out
    assert history.LATER in out and history.OFFER not in out


@pytest.mark.parametrize("value", ["", "1"])
def test_must_refuse_4_and_9_claudecode_set_to_anything_counts(monkeypatch, value):
    monkeypatch.setenv(history.CLAUDE_MARK, value)
    assert history.run_by_claude()
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    assert not cli.by_a_person()


def test_must_refuse_6_a_nested_transcript_from_elsewhere_keeps_the_folder(fake, layout):
    session(layout["ours"] / "a" / "subagents", str(fake["home"] / "elsewhere"), "agent-1")
    clean(fake, "yes")
    assert layout["ours"].exists()


def test_must_refuse_6_a_superseded_transcript_with_no_cwd_keeps_the_folder(fake, layout):
    (layout["ours"] / "a.jsonl.superseded-1").write_text('{"type": "x"}\n', encoding="utf-8")
    clean(fake, "yes")
    assert layout["ours"].exists()


def test_must_refuse_6_a_link_inside_the_repo_leading_out_isnt_inside(fake):
    repo = fake["repo"]
    elsewhere = fake["home"] / "elsewhere"
    elsewhere.mkdir()
    try:
        (repo / "linked").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this system won't make links")
    folder = project(fake, repo)
    session(folder, str(repo / "linked"))
    clean(fake)
    assert folder.exists()


def test_must_refuse_7_a_file_with_other_hard_links_keeps_the_folder(fake, layout):
    try:
        os.link(fake["data"] / "kept.md", layout["ours"] / "shared.md")
    except OSError:
        pytest.skip("this system won't make hard links")
    _, script = clean(fake, "yes")
    assert layout["ours"].exists()
    assert "other hard links" in script.text


def test_must_refuse_7_a_new_valid_session_after_the_list_keeps_the_folder(fake, layout):
    script = Answers()

    def ask_then_add(question):
        script.events.append(f"ASK {question}")
        session(layout["ours"], str(fake["repo"]), "late")  # valid, but not on the list
        return "yes"

    history.clean(ask_then_add, script.say, repo=fake["repo"], data=fake["data"])
    assert layout["ours"].exists()


def test_must_refuse_7_a_swapped_folder_after_the_list_is_left_alone(fake, layout):
    script = Answers()

    def ask_then_swap(question):
        script.events.append(f"ASK {question}")
        ours = layout["ours"]
        ours.rename(ours.with_name("moved-away"))
        session(ours, str(fake["repo"]), "a")  # a new folder in its place, same sessions
        session(ours, str(fake["repo"]), "b")
        return "yes"

    history.clean(ask_then_swap, script.say, repo=fake["repo"], data=fake["data"])
    assert layout["ours"].exists()
    assert "it changed while you read the list" in script.text


@pytest.mark.parametrize("where", ["data", "diffdata"])
def test_must_refuse_7_claude_folder_overlapping_a_protected_one_stops(fake, monkeypatch, where):
    protected = fake["data"] if where == "data" else fake["home"] / ".diffdata"
    folder = protected / "projects" / history.encode(fake["repo"])
    session(folder, str(fake["repo"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(protected))
    code, script = clean(fake, "yes")
    assert code == 1
    assert folder.exists()
    assert "overlaps" in script.text


def test_must_refuse_7_a_config_that_doesnt_load_stops_the_command(
    fake, layout, monkeypatch, capsys
):
    monkeypatch.setattr(cli.review, "in_terminal", lambda *streams: True)
    monkeypatch.setattr(history, "REPO", fake["repo"])
    (fake["home"] / ".diffdata" / "config.toml").write_text("not = [valid", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda q="": pytest.fail("it asked"))
    before = snapshot(fake["home"])
    assert cli.main(["clean-history"]) == 1
    assert "data folder isn't known" in capsys.readouterr().err
    assert snapshot(fake["home"]) == before


def test_must_refuse_7_a_linked_projects_folder_stops(fake, tmp_path):
    real = tmp_path / "real-projects"
    real.mkdir()
    session(real / history.encode(fake["repo"]), str(fake["repo"]))
    projects = fake["projects"]
    projects.rmdir()
    try:
        projects.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this system won't make links")
    code, script = clean(fake, "yes")
    assert code == 1
    assert (real / history.encode(fake["repo"])).exists()


def test_claude_codes_folder_names_written_out():
    # Written out by hand, not with encode(), so a change to encode() fails here.
    assert history.encode(PureWindowsPath(r"C:\Users\pat\differential-data-collection")) == (
        "C--Users-pat-differential-data-collection"
    )
    assert history.encode("/Users/pat/code/differential-data-collection") == (
        "-Users-pat-code-differential-data-collection"
    )
    assert history.encode("/home/pat/My Repo.v2") == "-home-pat-My-Repo-v2"


# --- Codex review 2 (#152) -------------------------------------------------------------------


def test_must_refuse_4_clean_itself_refuses_without_a_person(fake, layout, monkeypatch):
    monkeypatch.setattr(history, "by_a_person", REAL_BY_A_PERSON)
    monkeypatch.delenv(history.CLAUDE_MARK, raising=False)
    # Under pytest stdin isn't a terminal, so the real check refuses.
    before = snapshot(fake["home"])
    script = Answers("yes")
    code = history.clean(script.ask, script.say, repo=fake["repo"], data=fake["data"])
    assert code == 1
    assert script.asked == []
    assert snapshot(fake["home"]) == before


def test_must_refuse_4_clean_refuses_under_claude_even_in_a_terminal(fake, layout, monkeypatch):
    monkeypatch.setattr(history, "by_a_person", REAL_BY_A_PERSON)
    monkeypatch.setattr(history, "is_terminal", lambda stream: True)
    monkeypatch.setenv(history.CLAUDE_MARK, "1")
    script = Answers("yes")
    assert history.clean(script.ask, script.say, repo=fake["repo"], data=fake["data"]) == 1
    assert layout["ours"].exists()


def test_must_refuse_7_a_mount_point_inside_keeps_the_folder(fake, layout, monkeypatch):
    inner = layout["ours"] / "a"
    monkeypatch.setattr(history, "mount_points", lambda: [inner.resolve()])
    _, script = clean(fake, "yes")
    assert layout["ours"].exists()
    assert "mount point" in script.text


def test_mount_points_reads_escaped_paths(monkeypatch, tmp_path):
    info = tmp_path / "mountinfo"
    info.write_text(
        "36 35 98:0 /mnt1 /home/pat/my\\040disk rw - ext3 /dev/x rw\n", encoding="utf-8"
    )
    real_path = Path

    def fake_path(text, *rest):
        return info if text == "/proc/self/mountinfo" else real_path(text, *rest)

    monkeypatch.setattr(history, "Path", fake_path)
    assert history.mount_points() == [real_path("/home/pat/my disk")]


def test_must_refuse_7_a_replaced_session_after_the_list_keeps_the_folder(fake, layout):
    script = Answers()

    def ask_then_replace(question):
        script.events.append(f"ASK {question}")
        (layout["ours"] / "a.jsonl").unlink()
        session(layout["ours"], str(fake["repo"]), "z")  # same count, another session
        return "yes"

    history.clean(ask_then_replace, script.say, repo=fake["repo"], data=fake["data"])
    assert layout["ours"].exists()


def test_must_refuse_7_a_nested_transcript_added_after_the_list_keeps_the_folder(fake, layout):
    script = Answers()

    def ask_then_add(question):
        script.events.append(f"ASK {question}")
        session(layout["ours"] / "a" / "subagents", str(fake["repo"]), "agent-2")
        return "yes"

    history.clean(ask_then_add, script.say, repo=fake["repo"], data=fake["data"])
    assert layout["ours"].exists()


def test_must_refuse_7_a_folder_swapped_during_the_delete_is_put_back(fake, layout, monkeypatch):
    real_rename = os.rename
    swapped = {}

    def swap_then_rename(src, dst):
        if not swapped and Path(src) == layout["ours"]:
            swapped["done"] = True
            real_rename(src, fake["home"] / "moved-away")
            Path(src).mkdir()  # another folder appears at the checked name
            session(Path(src), str(fake["repo"]), "a")
        return real_rename(src, dst)

    monkeypatch.setattr(history.os, "rename", swap_then_rename)
    _, script = clean(fake, "yes")
    assert (fake["home"] / "moved-away" / "a.jsonl").exists()
    assert layout["ours"].exists()  # the stand-in was put back, not deleted
    assert "it changed while you read the list" in script.text


def test_must_refuse_6_a_folder_named_like_a_transcript_isnt_one(fake):
    folder = project(fake, fake["repo"])
    (folder / "notes.jsonl").mkdir(parents=True)
    _, script = clean(fake)
    assert folder.exists()
    assert "no session files to check" in script.text


def test_must_refuse_6_a_removed_folder_behind_a_link_leading_out_isnt_inside(fake):
    repo = fake["repo"]
    elsewhere = fake["home"] / "elsewhere"
    elsewhere.mkdir()
    try:
        (repo / "linked").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this system won't make links")
    folder = project(fake, repo)
    session(folder, str(repo / "linked" / "gone"))  # doesn't exist; its parent leads out
    clean(fake)
    assert folder.exists()


def test_a_removed_worktree_inside_the_repo_still_counts(fake):
    repo = fake["repo"]
    folder = project(fake, repo / ".claude" / "worktrees" / "old")
    session(folder, str(repo / ".claude" / "worktrees" / "old"))  # removed since
    clean(fake, "yes")
    assert not folder.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows folder names can't hold control characters")
def test_must_refuse_5_control_characters_in_a_name_are_shown_as_question_marks(fake):
    folder = fake["projects"] / (history.encode(fake["repo"]) + "-\x1b[2Jx")
    session(folder, str(fake["home"] / "elsewhere"))
    _, script = clean(fake)
    assert "\x1b" not in script.text
    assert "-?[2Jx" in script.text


def test_a_config_in_the_current_folder_gives_the_data_folder(fake, monkeypatch, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    (fake["home"] / ".diffdata" / "config.toml").unlink()
    (here / "config.toml").write_text("not = [valid", encoding="utf-8")
    with pytest.raises(history.HistoryError, match="data folder isn't known"):
        cli.history_data_dir()
    (here / "config.toml").unlink()
    assert cli.history_data_dir() is None
