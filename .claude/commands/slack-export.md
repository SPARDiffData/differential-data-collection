---
description: Export the Slack channels and canvases listed in config.toml to markdown in your data folder (DIS-1, COL-2). Read-only.
argument-hint: "[test]"
allowed-tools: mcp__claude_ai_Slack__slack_search_channels mcp__claude_ai_Slack__slack_read_channel mcp__claude_ai_Slack__slack_read_thread mcp__claude_ai_Slack__slack_read_canvas mcp__claude_ai_Slack__slack_search_public_and_private Bash(uv run diffdata discover slack *)
disallowed-tools: Write Edit NotebookEdit
hooks:
  PreToolUse:
    - matcher: "mcp__claude_ai_Slack__slack_(read_(channel|thread|canvas)|search_public_and_private)"
      hooks:
        - type: command
          command: uv
          args: ["run", "--directory", "${CLAUDE_PROJECT_DIR}", "diffdata", "discover", "slack", "check"]
          onFailure: block
  PostToolUse:
    - matcher: "mcp__claude_ai_Slack__slack_(read_(channel|thread|canvas)|search_public_and_private)"
      hooks:
        - type: command
          command: uv
          args: ["run", "--directory", "${CLAUDE_PROJECT_DIR}", "diffdata", "discover", "slack", "save"]
          onFailure: block
---

Export the Slack channels and canvases listed in `config.toml` to markdown in the user's data folder.

How it works: you read Slack through the Slack connector. Before each read or search, a hook checks that the run names the channel or canvas, and stops the call if it doesn't, or if the check itself can't run. After each `slack_read_channel`, `slack_read_thread` or `slack_read_canvas` call, a hook saves the result to the data folder, unchanged. After a files search, it saves only the sizes of the listed canvases. Then `diffdata` builds the export from what was saved.

After each read or search, the hook adds a note that starts "diffdata saved this result". It gives the number of messages or files, the next page's cursor, and, for a channel read, the posts that have replies. For a canvas, it gives the text's length. **Use the note to decide your next call.** A big result may show you only a preview that doesn't reach the cursor or every thread. The note covers the whole result, so don't open the saved file to look.

## Rules

- **Read-only.** Only read and search Slack. Never send, post, react, upload or edit anything, in Slack or anywhere else.
- **Don't copy results anywhere.** The hook saves them. Don't retype tool results into files, commands or your reply.
- **Report counts only.** Never repeat message or canvas text, file names, names, emails, channel or canvas IDs, or links from the results. They're private, and this session's output may be shared.
- **Stop on a hook error or a missing note.** If a hook stops a read with a `diffdata:` message, or a tool result comes back with a `diffdata:` error or with no "diffdata saved this result" note, stop. Run `uv run diffdata discover slack abort` and tell the user what happened, quoting the error if there was one.

Arguments: $ARGUMENTS

If the arguments say `test`, this is a **test run**: use only the first channel, read one page of 5 messages in step 3, skip step 4, search only the first page in step 5, and read only the first canvas in step 7.

## Steps

1. **Channels.** Run `uv run diffdata discover slack channels`. It prints the opted-in channel names from `config.toml`, one per line after the first. Don't open `config.toml` yourself. If the command fails, stop and show its message: it says where `config.toml` belongs.
2. **Open a run.** For each name, find its channel ID with `slack_search_channels`, with `keywords` set to the name and `channel_types` set to `public_channel,private_channel`. Use only a result whose name matches exactly; if there isn't one, stop and say which name wasn't found. Then run:
   ```
   uv run diffdata discover slack begin <name>=<ID> <name>=<ID> ...
   ```
   If it fails, stop and show its message. It says how many canvases `config.toml` lists.
3. **Read each channel.** Call `slack_read_channel` with the channel ID, `response_format: "detailed"` and `limit: 100`. While the note gives a next cursor, call it again with that `cursor`. This returns top-level posts only.
4. **Read each thread.** For every post the notes list as having replies, call `slack_read_thread` with the channel ID, that post's `ts` as `message_ts`, `response_format: "detailed"` and `limit: 1000`, following any cursor the same way.
5. **Search for canvas sizes.** Skip this step and the next two if `begin` said 0 canvases. For each channel ID, call `slack_search_public_and_private` with exactly these: `content_types: "files"`, `filters: "in:<#ID>"` (the channel ID in place of `ID`), `channel_types: "public_channel,private_channel"`, `natural_language_query: ""`, `response_format: "detailed"`, `include_context: false` and `limit: 20`. While the note gives a next cursor, call it again with the same values and that `cursor`. Don't search Slack any other way: the check before each search would stop the export.
6. **Which canvases to read.** Run `uv run diffdata discover slack canvases-due`. It ends with the canvas IDs to read, or says there are none. If it fails, stop and show its message.
7. **Read each canvas it lists.** For each canvas ID that `canvases-due` printed, call `slack_read_canvas` with that ID as `canvas_id`. Read only those. Don't read a canvas's comment threads, even if the result lists them: they come in a later step (COL-3), and the check before each read would stop the export. A canvas is often too big to show you, and the result then says it was saved to a file and asks you to read that file: **don't**. The hook has already saved it, and the note covers it. Don't summarize or quote a canvas.
8. **Build.** Run `uv run diffdata discover slack build` and show the user its output, which is counts only.
