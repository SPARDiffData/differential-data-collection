---
description: Export the Slack channels listed in config.toml to markdown in your data folder (DIS-1). Read-only.
argument-hint: "[test]"
allowed-tools: mcp__claude_ai_Slack__slack_search_channels mcp__claude_ai_Slack__slack_read_channel mcp__claude_ai_Slack__slack_read_thread Bash(uv run diffdata discover slack *)
disallowed-tools: Write Edit NotebookEdit
hooks:
  PostToolUse:
    - matcher: "mcp__claude_ai_Slack__slack_read_(channel|thread)"
      hooks:
        - type: command
          command: uv
          args: ["run", "--directory", "${CLAUDE_PROJECT_DIR}", "diffdata", "discover", "slack", "save"]
---

Export the Slack channels listed in `config.toml` to markdown in the user's data folder.

How it works: you read Slack through the Slack connector. After each `slack_read_channel` or `slack_read_thread` call, a hook saves the result to the inbox in the data folder, unchanged. Then `diffdata` builds the export from the inbox.

## Rules

- **Read-only.** Only read and search Slack. Never send, post, react, upload or edit anything, in Slack or anywhere else.
- **Don't copy results anywhere.** The hook saves them. Don't retype tool results into files, commands or your reply.
- **Report counts only.** Never repeat message text, names, emails, channel IDs or links from the results. They're private, and this session's output may be shared.
- **Stop on a hook error.** If a tool result comes back with a `diffdata:` error from the hook, stop. Run `uv run diffdata discover slack abort` and tell the user what the error said.

Arguments: $ARGUMENTS

If the arguments say `test`, this is a **test run**: use only the first channel, read one page of 5 messages in step 3, and skip step 4.

## Steps

1. **Channels.** Read `config.toml` and take the channel names from `slack_channels` under `[sources]`. If the file or the list is missing, stop and tell the user to copy `config.example.toml` to `config.toml` and fill it in.
2. **Open a run.** For each name, find its channel ID with `slack_search_channels`, with `keywords` set to the name and `channel_types` set to `public_channel,private_channel`. Use only a result whose name matches exactly; if there isn't one, stop and say which name wasn't found. Then run:
   ```
   uv run diffdata discover slack begin <name>=<ID> <name>=<ID> ...
   ```
   If it fails, stop and show its message.
3. **Read each channel.** Call `slack_read_channel` with the channel ID, `response_format: "detailed"` and `limit: 100`. While the result gives a next cursor, call it again with that `cursor`. This returns top-level posts only.
4. **Read each thread.** For every top-level post in step 3's results that has replies, call `slack_read_thread` with the channel ID, the post's `ts` as `message_ts`, `response_format: "detailed"` and `limit: 1000`, following any cursor the same way.
5. **Build.** Run `uv run diffdata discover slack build` and show the user its output, which is counts only.
