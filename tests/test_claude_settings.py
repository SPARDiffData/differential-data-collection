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


def test_the_hook_saves_channel_and_thread_reads_only():
    matcher = re.search(r'matcher: "(.+)"', command_frontmatter()).group(1)
    saved = [t for t in SLACK_READ_TOOLS + SLACK_WRITE_TOOLS if re.fullmatch(matcher, SLACK + t)]
    assert saved == ["slack_read_channel", "slack_read_thread"]


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
    "Bash(gh auth status:*)",
    "Bash(gh api user:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Read",
    "Grep",
    "Glob",
}


def test_plan_issue_pre_approves_reads_only():
    line = re.search(r"^allowed-tools: (.+)$", command_frontmatter("plan-issue"), re.MULTILINE)
    assert set(line.group(1).split(", ")) == PLAN_ISSUE_READS
