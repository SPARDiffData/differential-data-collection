"""Read-only, enforced: the repo's Claude settings deny every Slack write tool (DIS-1).

`.claude/settings.json` holds the deny list, which Claude Code applies in every permission
mode. A command's `allowed-tools` only pre-approves tools; it doesn't restrict anything,
so these tests also check it lists read and search tools only.

fnmatch here approximates Claude Code's own matching. The real check is a fresh session:
the write tools below must not appear in it at all.
"""

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SLACK = "mcp__claude_ai_Slack__"
# Every write tool the Slack connector offered on 2026-10-09.
SLACK_WRITE_TOOLS = [
    "slack_add_list_record",
    "slack_add_reaction",
    "slack_complete_file_upload",
    "slack_create_canvas",
    "slack_create_conversation",
    "slack_create_list",
    "slack_get_file_upload_url",
    "slack_schedule_message",
    "slack_send_message",
    "slack_send_message_draft",
    "slack_update_canvas",
    "slack_update_list",
    "slack_update_list_record",
]
# Made-up names for write tools the connector might add later, one per write verb.
FUTURE_SLACK_WRITE_TOOLS = [
    "slack_edit_message",
    "slack_post_message",
    "slack_pin_message",
    "slack_invite_user",
    "slack_join_channel",
    "slack_archive_channel",
    "slack_set_channel_topic",
    "slack_mark_read",
    "slack_rename_channel",
]
SLACK_READ_TOOLS = [
    "slack_get_reactions",
    "slack_list_channel_members",
    "slack_list_user_channels",
    "slack_read_canvas",
    "slack_read_channel",
    "slack_read_file",
    "slack_read_list",
    "slack_read_thread",
    "slack_read_user_profile",
    "slack_search_channels",
    "slack_search_emojis",
    "slack_search_public",
    "slack_search_public_and_private",
    "slack_search_users",
]


def deny_rules():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return settings["permissions"]["deny"]


def denied(tool):
    return any(fnmatchcase(tool, rule) for rule in deny_rules())


def command_frontmatter(command="slack-export"):
    text = (ROOT / ".claude" / "commands" / f"{command}.md").read_text(encoding="utf-8")
    return text.split("---")[1]


def frontmatter_line(name, command="slack-export"):
    match = re.search(rf"^{name}: (.+)$", command_frontmatter(command), re.MULTILINE)
    return match.group(1).split()


@pytest.mark.parametrize("tool", SLACK_WRITE_TOOLS)
def test_every_slack_write_tool_is_denied(tool):
    assert denied(SLACK + tool)


@pytest.mark.parametrize("tool", FUTURE_SLACK_WRITE_TOOLS)
def test_future_slack_write_verbs_are_denied(tool):
    assert denied(SLACK + tool)


@pytest.mark.parametrize("tool", SLACK_READ_TOOLS)
def test_slack_read_tools_stay_available(tool):
    assert not denied(SLACK + tool)


def test_slack_export_pre_approves_only_read_and_search_tools():
    allowed = frontmatter_line("allowed-tools")
    connector_tools = [t for t in allowed if t.startswith("mcp__")]
    assert connector_tools, "the command should pre-approve its Slack read tools"
    for tool in connector_tools:
        assert tool.startswith(SLACK)
        assert tool.removeprefix(SLACK) in SLACK_READ_TOOLS
        assert not denied(tool)


def test_slack_export_removes_the_file_editing_tools():
    assert {"Write", "Edit", "NotebookEdit"} <= set(frontmatter_line("disallowed-tools"))


def slack_export_hooks():
    """Each hook event in /slack-export's frontmatter: its matcher, its diffdata step and
    what Claude Code does if the hook fails."""
    found = re.findall(
        r'^  (\w+):\n    - matcher: "([^"\n]+)"\n(?:.*\n)*?.*"slack", "(\w+)"\]\n'
        r"(?:\s+onFailure: (\w+)\n)?",
        command_frontmatter(),
        re.MULTILINE,
    )
    return {event: (matcher, step, on_failure) for event, matcher, step, on_failure in found}


SAVED_READS = [
    "slack_read_canvas",
    "slack_read_channel",
    "slack_read_thread",
    "slack_search_public_and_private",
]
EVERY_SLACK_TOOL = sorted(SLACK_READ_TOOLS + SLACK_WRITE_TOOLS + FUTURE_SLACK_WRITE_TOOLS)


@pytest.mark.parametrize(
    "event, step, tools",
    [("PreToolUse", "check", EVERY_SLACK_TOOL), ("PostToolUse", "save", SAVED_READS)],
)
def test_the_check_sees_every_slack_tool_and_the_save_the_reads_it_saves(event, step, tools):
    # CON-1: every Slack call is checked before it runs, so check() can stop the ones the
    # export never makes (#65), and each saved result is checked before it's saved.
    matcher, hook_step, on_failure = slack_export_hooks()[event]
    assert hook_step == step
    # A hook that can't start, crashes or times out blocks the read too. Without this, Claude
    # Code carries on as if the check had passed (needs Claude Code 2.1.295 or later).
    assert on_failure == "block"
    matched = [t for t in EVERY_SLACK_TOOL if re.fullmatch(matcher, SLACK + t)]
    assert matched == tools


# CON-1: approvals are typed by a person in a terminal, never by Claude. The consent commands
# refuse outside a terminal, and these rules also stop Claude's shell tools running them,
# including through a program that fakes a terminal, such as `script` on macOS and Linux.
CONSENT_RUNS = [
    "Bash(uv run diffdata consent review)",
    "Bash(echo all | script -q /dev/null uv run diffdata consent review)",
    "Bash(.venv/bin/diffdata consent withdraw)",
    "Bash(uv run python -m diffdata consent review)",
    'Bash(uv run python -c "from diffdata.consent import review")',
    'Bash(uv run python -c "import diffdata.consent.review")',
    "PowerShell(uv run diffdata consent review)",
    "PowerShell(.venv\\Scripts\\diffdata.exe consent withdraw)",
]
DEVELOPMENT_RUNS = [
    "Bash(uv run pytest tests/test_consent.py)",
    "Bash(git add src/diffdata/consent/review.py)",
    "Bash(uv run diffdata discover slack build)",
    "PowerShell(uv run pytest -k consent)",
]


@pytest.mark.parametrize("command", CONSENT_RUNS)
def test_claudes_shell_tools_cant_run_the_consent_commands(command):
    assert denied(command)


@pytest.mark.parametrize("command", DEVELOPMENT_RUNS)
def test_working_on_the_consent_code_isnt_blocked(command):
    assert not denied(command)


# CON-1: an "approved" line in the approvals file, or a canvas listed in config.toml, opts an
# item in, so Claude's file tools can't write either one. Edit rules also cover a shell
# redirect into the file, but not PowerShell or a script that opens the file itself.
OPT_IN_EDITS = ["Edit(~/.diffdata/approvals.jsonl)", "Edit(~/.diffdata/config.toml)"]
DEVELOPMENT_EDITS = ["Edit(config.example.toml)", "Edit(src/diffdata/consent/approvals.py)"]


@pytest.mark.parametrize("edit", OPT_IN_EDITS)
def test_claudes_file_tools_cant_write_the_files_that_opt_items_in(edit):
    assert denied(edit)


@pytest.mark.parametrize("edit", DEVELOPMENT_EDITS)
def test_editing_the_consent_code_and_the_example_config_isnt_blocked(edit):
    assert not denied(edit)


def test_no_deny_rule_names_a_path_claude_code_never_checks():
    # Claude Code checks a file path against Edit and Read rules only. A Write(...) or
    # NotebookEdit(...) rule would look like protection and block nothing.
    assert not [r for r in deny_rules() if re.match(r"(Write|NotebookEdit|MultiEdit)\(", r)]


def test_slack_export_never_runs_consent_review():
    # CON-1: approvals are typed by a person in a terminal. The command pre-approves only its
    # own export steps, and tells Claude never to run review.
    assert not [tool for tool in frontmatter_line("allowed-tools") if "consent" in tool]
    body = (ROOT / ".claude" / "commands" / "slack-export.md").read_text(encoding="utf-8")
    assert "run `uv run diffdata consent review`. **Never run it yourself.**" in body


def test_slack_export_reads_canvases_but_cant_write_them():
    # COL-2: the command pre-approves the canvas read, and the deny list blocks every write.
    assert SLACK + "slack_read_canvas" in frontmatter_line("allowed-tools")
    # COL-8: and the files search that says which canvases changed.
    assert SLACK + "slack_search_public_and_private" in frontmatter_line("allowed-tools")
    for tool in ("slack_create_canvas", "slack_update_canvas"):
        assert denied(SLACK + tool)


def test_review_pr_removes_the_file_editing_tools():
    # ENG-5: the review only reports, so it runs without Write and Edit.
    assert {"Write", "Edit", "NotebookEdit"} <= set(
        frontmatter_line("disallowed-tools", "review-pr")
    )


# ENG-5: /plan-issue may run these read-only tools without asking. Posting the plan to the
# Issue is left out on purpose, so it's never pre-approved.
PLAN_ISSUE_READS = {
    "Bash(gh issue view:*)",
    "Bash(gh pr view:*)",
    "Bash(gh auth status)",  # exactly this: "gh auth status:*" also allows --show-token
    "Bash(gh api user --jq .login)",  # not "gh api user:*", which also allows -X PATCH
    "Read",
    "Grep",
    "Glob",
}
# ENG-5: the same for /review-pr. "git diff:*" and "git log:*" would also allow --output,
# which writes a file, so the fallback commands are pre-approved in exactly the form it uses.
REVIEW_PR_READS = {
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh issue view:*)",
    "Bash(gh auth status)",
    "Bash(git fetch origin)",
    "Bash(git diff origin/main...HEAD)",
    "Read",
    "Grep",
    "Glob",
}


# ENG-5: the same for /debrief, which reads the PR or this branch's changes and publishes a page.
DEBRIEF_READS = {
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh issue view:*)",
    "Bash(git log --oneline main..HEAD)",
    "Bash(git diff --stat main...HEAD)",
    "Bash(git diff main...HEAD)",
    "Read",
    "Grep",
    "Glob",
}


def pre_approved(command):
    line = re.search(r"^allowed-tools: (.+)$", command_frontmatter(command), re.MULTILINE)
    return set(line.group(1).split(", "))


def test_plan_issue_pre_approves_reads_only():
    assert pre_approved("plan-issue") == PLAN_ISSUE_READS


def test_review_pr_pre_approves_reads_only():
    assert pre_approved("review-pr") == REVIEW_PR_READS


def test_debrief_pre_approves_reads_only():
    assert pre_approved("debrief") == DEBRIEF_READS


def test_debrief_cant_edit_code():
    # It writes one page outside the repo, so Write stays; Edit isn't needed.
    assert {"Edit", "NotebookEdit"} <= set(frontmatter_line("disallowed-tools", "debrief"))


def test_review_pr_posts_without_writing_a_file():
    # It can't use Write, so it posts from the command line rather than saving to a file first.
    body = (ROOT / ".claude" / "commands" / "review-pr.md").read_text(encoding="utf-8")
    assert "--body-file -" in body
    assert "temporary file outside the repo" not in body


# ENG-5: the review agent (.claude/agents/). Agent files list tools comma-separated.
AGENTS = ROOT / ".claude" / "agents"
FILE_EDITING_TOOLS = {"Write", "Edit", "NotebookEdit"}


def agent_tools(name, agent="pr-reviewer"):
    frontmatter = (AGENTS / f"{agent}.md").read_text(encoding="utf-8").split("---")[1]
    line = re.search(rf"^{name}: (.+)$", frontmatter, re.MULTILINE)
    return {tool.strip() for tool in line.group(1).split(",")}


def test_every_agent_file_is_covered_by_these_tests():
    assert sorted(p.stem for p in AGENTS.glob("*.md")) == ["pr-reviewer"], (
        "A new agent file needs its own tests here."
    )


def test_the_review_agent_has_no_file_editing_tools():
    # Neither given nor inherited: disallowedTools also removes them from anything inherited.
    assert FILE_EDITING_TOOLS <= agent_tools("disallowedTools")
    assert not FILE_EDITING_TOOLS & agent_tools("tools")


def test_the_review_agent_has_only_the_tools_review_pr_needs():
    # Skill runs /review-pr; Bash runs its read-only gh and git commands.
    assert agent_tools("tools") == {"Skill", "Bash", "Read", "Grep", "Glob"}


def test_the_review_agent_runs_review_pr_without_posting():
    agent = (AGENTS / "pr-reviewer.md").read_text(encoding="utf-8").split("---", 2)[2]
    review_pr = (ROOT / ".claude" / "commands" / "review-pr.md").read_text(encoding="utf-8")
    assert "`review-pr`" in agent and "--no-post" in agent
    assert "**With `--no-post`,**" in review_pr


def test_review_pr_asks_for_a_box_the_pr_template_has():
    # /review-pr checks Paul's consent, scrub and store PRs for this box by its opening words.
    template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    review_pr = (ROOT / ".claude" / "commands" / "review-pr.md").read_text(encoding="utf-8")
    box = "Paul's own consent, scrub or store PR"
    assert f"- [ ] {box}:" in template
    assert f'box that starts "{box}"' in review_pr
    assert "Solo phase:" not in template


def test_review_pr_reads_a_contributing_section_that_exists():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    review_pr = (ROOT / ".claude" / "commands" / "review-pr.md").read_text(encoding="utf-8")
    section = "Human review for consent, scrub and store"
    assert f'including "{section}"' in review_pr
    assert f"\n### {section}\n" in contributing
    assert "(#human-review-for-consent-scrub-and-store)" in contributing
