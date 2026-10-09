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

`slack_channel_sample.md` is a small illustrative channel. Its shape isn't the export format; DIS-1 will define that.
