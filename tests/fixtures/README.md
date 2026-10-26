# Test fixtures

**Synthetic data only.** Everything in this folder is made up: people, messages, links and IDs.
Never copy real collected data here, not even a trimmed or "anonymized" sample.

When you add a fixture:

- Invent names (Alex Example, Sam Sample) and use `example.com` for email addresses. A test fails on any other email domain.
- Save text fixtures as UTF-8. The test fails on text it can't read, because that's where a real address could hide.
- The test can't look inside binary fixtures (`.docx`, `.pdf`, images, recordings), so check those by hand before committing. The types it skips are listed in `BINARY_SUFFIXES` in `tests/test_no_real_data.py`.
- If the test says a file is "not UTF-8, so it can't be checked":
  - If it's meant to be binary, and its type isn't in `BINARY_SUFFIXES` yet (say `.gif`, `.zip` or `.webm`), add the type there and check the file by hand.
  - If it's text, save it as UTF-8. In VS Code, click the encoding in the bottom bar, choose "Save with Encoding", then "UTF-8".
- Use obviously fake IDs and links (`FAKE-DOC-0001`, `T00000000`).
- Don't put anything that looks like a real token or key here. GitHub's push protection may block it, and fake secrets for scrub tests need their own agreed approach (SCR-1).

## `slack/`

Made-up Slack connector results, for the DIS-1 export tests:

- `read_channel.txt` and `read_thread.txt` copy the exact layout of what `slack_read_channel` and `slack_read_thread` return in detailed mode, with every name, ID, link and message invented. `tests/conftest.py` wraps them the way the connector does: a list of text blocks holding JSON, whose `messages` field is the file's text.
- `read_channel_new.txt`, `read_thread_late.txt` and `search_messages.json` are a later, incremental run: one new post, the same thread with a late reply, and the message search (`content_types: messages`) that finds that reply. The search copies the layout of the connector's `results` text, with every value invented.
- `expected/` holds the markdown view the export should build from them, one file per day. If you change the view on purpose, update these files and check the difference by eye.
- `references_channel.txt`, `references_thread.txt` and `references_canvas.json` are a channel read, its thread and a canvas read that point to every kind of source the reference log knows: a canvas, Slack files and attachments, Google Docs, Sheets, Slides and Drive files, a Drive folder, ordinary links, a Slack message link, an email link and a canvas-tab notice. `references_expected.md` is the `references.md` the build should write from them. Its IDs start at `F00000010` so they don't clash with the files above.

To learn the layout, run a live read and look at it on your own computer, then write the fixture by hand. Never paste a real result into this folder, even for a moment.
