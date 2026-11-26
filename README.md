# differential-data-collection

`diffdata` finds the working material behind research (Slack threads, canvases, Google Docs, transcripts), collects it, scrubs it and stores it centrally. It's the collection tool for the SPAR Fall 2026 project *Differential Data for Automated AI Safety Research*. We're testing it on our own team's data first.

## Status

**Pilot-ready, still at version 0.0.1.** These work today: setup from a team file, the Slack export through Claude Code (with opt-outs and late joiners left out), Slack canvases, Google Docs, the consent review, the scrub, and the push to your team's folder on Google Cloud Storage. Per-use opt-outs and consumer access aren't built yet. The version number hasn't moved since the first skeleton: see [CHANGELOG.md](CHANGELOG.md).

## Quick start

### Before you start

| You need | Why | How to check |
|---|---|---|
| [Git](https://git-scm.com/downloads) | to get the tool | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | runs the tool, and installs Python 3.12 for it | `uv --version` |
| [Claude Code](https://claude.com/claude-code) 2.1.295 or later: the `claude` command, or the Claude desktop app | runs `/slack-export` with this repo's safety rules | `claude --version`, or the app's About screen |
| The Slack connector in Claude, signed in to your workspace | reads your channels, as you | in Claude: `/mcp` |
| A Google account | only if you collect Google Docs | |
| The team file | your team's settings and upload key | from the project team |

Once the tool is installed (below), `uv run diffdata check` checks the first three, that you're in the repo folder, and that setup is done. It changes nothing. Setup runs the same checks first, and lists anything missing at the end. A terminal can't see Claude's connectors, so the Slack connector and the Google account aren't checked.

### Install with one line

On a Mac or Linux, in Terminal (put your team file's path at the end, or leave it off and setup asks for it):

```bash
curl -fsSL https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.sh | bash -s -- ~/Downloads/DD-7KQ4.diffdata-team.json
```

On Windows, in PowerShell (not as administrator):

```powershell
$env:DIFFDATA_TEAM_FILE = "$HOME\Downloads\DD-7KQ4.diffdata-team.json"; irm https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.ps1 | iex
```

It first checks the folder it will use, `differential-data-collection` in your home folder (set `DIFFDATA_REPO` for another): if something is there that isn't a clean clone of this repo on `main` (no changes or files of yours), it stops with nothing changed. Then:

- **Git:** on a Mac, if Git is missing, it starts Apple's installer and stops: run the line again when that's done. On Linux it stops and asks you to install Git. On Windows it installs Git with winget, which may ask you to allow it.
- **uv:** if it's missing, it installs it from Astral, which also adds uv to your PATH so `uv run` works in new windows.
- **The repo:** it clones it, or fast-forwards your clean clone, then runs `uv sync` (Python packages from PyPI, as locked).
- **Setup:** it starts `diffdata setup` with your team file. It never opens the team file: it only passes its path. If it isn't in a terminal you type in, or Claude Code runs it, it prints the setup line for you to run instead. Setup itself checks the rest (`diffdata check`) before it asks anything.

It never runs as root or administrator and never uses `sudo`.

### Or install by hand

```
git clone https://github.com/SPARDiffData/differential-data-collection
cd differential-data-collection
uv sync
uv run diffdata --help
uv run pytest
```

The project team sends you your team's **project code** (like `DD-7KQ4`), the **channel notice** and the **mentor message**, and a **team file** (`<code>.diffdata-team.json`). Do these in this order:

1. **Post the notice in each channel you'll collect, and pin it.** Replace each `[close date]` with the date 7 days after the day you post. The 7 days start when it's posted.
2. **Send the mentor message** to your mentor, in Slack.
3. **Run setup once,** with the team file, in your own Terminal or PowerShell, in the repo folder. Have each pinned notice's link ready (the notice's menu, Copy link): setup asks for it.

```
uv run diffdata setup ~/Downloads/DD-7KQ4.diffdata-team.json
```

Setup asks for the Slack channels to collect (type a name, or paste the channel's link), and for your workspace's address if no link gives it. Then it asks for each channel's notice link: paste the link of the notice pinned in that channel (its menu, Copy link), type `none` if everyone in it agreed directly, or press Enter to add it later. A link must be a Slack message link from that channel and workspace, or setup asks again; it checks this from the link itself, without going online. Until a channel has a notice link or `none`, `/slack-export` stops at it, and `uv run diffdata check` lists it: when the notice is posted, run setup again, answer `y` to Change it, and paste the link. It asks whether to send copies not approved for release (see "Decide about copies not approved for release" below): type `y` or `n`, since Enter alone asks again, on every run that writes your settings. It chooses the rest: your data folder, a made-up producer handle, and the team values in the file (bucket, folder, project). It shows everything on one screen, with each channel's notice as its date and time, "none" or "not added yet": type `y` to write it, `n` to stop, or `c` to go through each answer in turn. Then it makes your data folder, and writes `config.toml` in a `.diffdata` folder in your home folder (`C:\Users\<you>\.diffdata\` on Windows, `~/.diffdata/` on a Mac). It puts the team's store key and Google client file there too, readable only by you, and never shows them. It deletes the team file only if you say yes. Direct messages and group DMs can't be listed: the other people in them never agreed. Run it again to change your answers: it asks before it changes anything, keeps every other setting, and keeps the old file as `config.toml.bak`. At each notice question, Enter keeps the channel's notice as it is, and a channel you take off the list keeps its entry. Setup only runs in a terminal you type in, and this repo's settings stop Claude from running it, because the channel list is your opt-in.

**Your producer handle is a pseudonym.** It labels your copies in the shared store instead of your name, so it's never your real name. Setup offers a made-up one, such as `example-project-k4r7`; press Enter to take it.

**For the project team:** the team file is JSON: `{"format": "diffdata-team", "version": 1, "bucket": "…", "folder": "…", "project": "…", "store_key": {…the team's service account key…}, "google_client": {…the Desktop app client…}}`. `google_client` may be left out; any other key is refused. Its `folder` is the team's project code. `diffdata admin add-team` makes the code, writes the file, and prints the channel notice and the mentor message: see "For the project team" in [CONTRIBUTING.md](CONTRIBUTING.md#for-the-project-team). It holds the team's key, so send it privately, never in the repo, an Issue, a PR or a shared channel. Claude's tools may not read files named `*.diffdata-team.json`.

You can still copy `config.example.toml` to `~/.diffdata/config.toml` and fill it in by hand. Also copy `.env.example` to `.env` there. Both stay on your computer. Keeping them outside the repo means tools working in the repo, such as Codex, can't read them.

## After day 7: one command

When every channel's 7 days are over, close Claude Code and type this in your own Terminal or PowerShell, in the repo folder:

```
uv run diffdata export
```

It does every step below in order, and says what happens next at each one:

1. **Checks** that setup is done, that the 7 days are over, and that your team's opt-out list is in your team's folder. Nothing is read before these pass.
2. **The test run, then the export.** Claude Code opens with `/slack-export test`. When it has finished, type `/exit`. If the test worked, it asks `Export now? [y/n]`, and on `y` Claude Code opens again with `/slack-export`: type `/exit` when it's done. Claude Code may ask you to sign in the first time: use the same Claude account as your Claude app, since your Slack connector is on that account.
3. **The review starts by itself**, as `uv run diffdata consent review` (see "Approve new canvases and Google files" below).
4. **More rounds.** If you approved canvases, Claude Code opens once more to read them. If Google Docs are listed or approved, Google's sign-in opens in your browser the first time, and the docs are fetched. Then the review again. Press Enter to go on, or type `stop`. It stops after 3 rounds.
5. **Scrub.** It shows the counts, and where the copies and `private/flags.md` are.
6. **Push.** Read the copies, then type `push` to upload them. Anything else sends nothing.
7. **Clear Claude's history.** It runs `diffdata clean-history`, which shows what it would delete and asks for `yes`.

**If you stop partway** (`n`, `stop`, Ctrl+C or closing the window), your review answers so far are kept, and nothing more is sent. Run `uv run diffdata export` again: if the export is done since your last push, it starts at the review. The push question is always asked again. It prints counts only. It runs only in a terminal you type in, never with Claude Code's shell, and this repo's settings stop Claude from running it. Claude Code gets only the slash command, with this repo's rules.

The sections below are the same steps one at a time, which still work.

## Export a Slack channel (DIS-1)

For the pilot there's no Slack app: Claude Code reads Slack for you through its Slack connector, as you. Nothing writes to Slack. This repo's `.claude/settings.json` removes every Slack write tool from Claude while it works in this folder. During `/slack-export`, a hook also stops any read of a channel or canvas that the export run doesn't name, any search other than a files or message search of one of the run's channels, and any Slack read the export never makes, such as a file download, before it runs. The run takes its channels from `config.toml`, and its canvases from `config.toml` and your approvals. If that check can't run, the read is stopped too. The check stays on for the rest of that Claude session, so start a new session for each export, and for any other Slack work afterwards. This needs Claude Code 2.1.295 or later (`claude --version`).

1. **Set up `config.toml`** in `~/.diffdata/`, with `uv run diffdata setup` (see above) or by hand. List the channels to export under `slack_channels`, by name, by ID (`C...`) or by link (`https://<workspace>.slack.com/archives/C...`); listing a channel is your opt-in. For a channel listed by ID or link, the first export reads its name from the channel itself. Edit the file yourself: this repo's settings stop Claude from changing it. Set `slack_workspace_url` to your workspace's address. Keep `data_dir` outside the repo and outside OneDrive, Dropbox, Google Drive and iCloud: `diffdata` refuses those.
2. **Try it.** Open Claude Code in the repo folder and type `/slack-export test`. It reads only the 5 newest messages of the first channel, and skips threads.
3. **Run it.** Type `/slack-export`. Claude reads each channel, searches it for late replies, and reads the threads. A hook saves each result unchanged to your data folder, so Claude never retypes it, and then `diffdata` builds:
   - `slack/export/<channel>/<date>.md`: one readable file per day (UTC), with thread replies under their post
   - `slack/store/<channel-id>/messages.jsonl`: every version of every message, for the later steps
   - `references/references.md`: every doc, canvas, attachment and link the saved messages and canvases point to, once each, with where and when it was shared, and whether it's collected. Nothing in it is fetched, and attachments are never downloaded. New canvases and Google files in it wait for your review: see below.

Claude reports counts only. The raw results, including authors' emails, stay in your data folder until scrubbing (SCR-2).

**People who opted out.** An export won't start until 7 days after each of its channels' notices (the time is in the notice's link, under `slack_notices`). When the 7 days are over, the project team looks at the form's answers and puts your team's opted-out Slack IDs in your team's folder. With `bucket` and `folder` under `[store]`, every export then fetches that list before it starts, and refuses to start if it can't: if the list isn't there yet, can't be read, or was written before the 7 days ended. A missing list is never read as "nobody opted out". The fetched IDs are kept in `slack/team_opted_out.json` in your data folder, which only grows, and are added to any you list under `slack_opted_out` in `config.toml`. Without a team folder, an export of a channel with a notice is refused, and for channels set to `"none"` only `slack_opted_out` applies. With a team folder, the save hook, the build and `diffdata push` also refuse while the fetched list is missing or was fetched for another folder. The build then leaves their messages out of the message store, the daily files and the reference log, and removes any of their lines the store already had. Replies others wrote to their posts stay, shown as posts of their own. Mentions of them in others' messages are pseudonymised by the scrub like everyone else's: their ID and display name are kept in `slack/left_out.json` in your data folder for that alone. The build reports how many messages it left out, never whose. After you change `slack_opted_out`, run `uv run diffdata discover slack build` before scrubbing: the scrub refuses to run while the saved data still holds their messages. The list must sit under `[sources]`: anywhere else, or misspelt, it stops the run rather than being ignored. The save hook also cuts their messages out of the raw results before saving them: each keeps only its author ID, shown as "Opted out", its time, and whether it has replies, so others' replies to it are still read. If a result can't be cut cleanly, for example because someone pasted Slack's own header lines into a message, or cutting would change anyone else's message, nothing from it is saved and the export stops, so tell the project team. A stopped read never moves the channel's start point, so later runs stop at the same message until the team has looked at it. Their display name can still appear inside others' messages in the raw results, as a mention, until the scrub replaces it. Their messages still reach Claude while it reads the channel: that can't be avoided while collection runs through the connector, and the privacy notice says so. Canvases can't be filtered by author, so skip any canvas an opted-out person helped write.

**People who joined after the notice.** They never got the notice or its 7 days, so their messages are left out too. Under `slack_notices` in `config.toml`, put each channel's notice link: open the pinned notice's menu and choose Copy link. `diffdata setup` asks for it (see above). Slack's history shows each join as "has joined the channel", and anyone whose latest join is after the notice is left out of that channel exactly like someone in `slack_opted_out`, including someone who left and came back. Only the person's own join counts, so nobody can make someone else a late joiner by typing that line. Key each entry the way `slack_channels` lists the channel: by its name, or by its ID or link (as `diffdata setup` writes them). Every channel you export needs an entry, or the build stops: write `"none"` for a channel with no notice, such as one where everyone agreed directly, and the build reports how many channels weren't checked. A join is only seen if a read reached back to it, so messages newer than an hour before the channel's last complete export began (or than the notice, before the first one) are held back until a complete `/slack-export` covers them: a test run or a read cut short adds nothing after the notice. A channel you've taken off `slack_channels` keeps its entry, since its saved data is still built. As with the opt-out list, run `uv run diffdata discover slack build` after changing `slack_notices`: the scrub, and then `diffdata push`, refuse until a build has applied it. Setup keeps your `slack_notices` when you run it again. The save hook doesn't do this check, since a channel read comes newest first and shows someone's messages before their join, so their raw messages stay in your data folder, but never reach the store, the daily files, the reference log or the scrubbed copies.

By hand: `uv run diffdata discover slack build` rebuilds the export from what's saved, and `uv run diffdata discover slack abort` stops a run. A run also closes on its own after an hour. If Claude says `uv` isn't found, restart VS Code.

**Only what's new.** The first complete export reads each channel from the start. After that, each run reads a channel only from an hour before the last complete run began. The hour covers clock differences and delays in Slack's search. A message search of the same channel finds new replies in older threads, and Claude then reads each of those threads again in full. The search's results aren't saved, only the threads it found replies in. A channel's start point moves forward only when a run read every page, finished the search, and read every thread listed. Otherwise the build says so, in counts, and the next run reads the same stretch again, so a skipped page can't leave a gap. A test run skips most of that, so it leaves the start point where it was. Each channel's start point is in `slack/state.json` in your data folder. If that file is missing, as on the first run after updating, the run works the start points out again from what's already saved.

**Edits to older posts** aren't seen by an incremental run. Type `/slack-export full` now and then: it reads every channel from the start, and every listed canvas.

## Collect Slack canvases (COL-2)

`/slack-export` also reads the canvases you list, after the channels, but only the ones that may have changed.

1. **List them** under `slack_canvases` in `config.toml`, by ID (`F...`) or by link (`https://<workspace>.slack.com/docs/T.../F...`). Listing a canvas is your opt-in: no other canvas is read. The same canvas listed twice is refused, since it's usually a paste of the wrong line.
2. **Run `/slack-export`** as above. `/slack-export test` reads at most one canvas. The build then writes, in your data folder:
   - `documents/current/slack-canvas_<title>_<id>.md`: each canvas's latest text, with mentions shown as names
   - `documents/slack-canvas/<id>/versions/`: one file per distinct text, so a new one appears only when the canvas changed
   - `documents/slack-canvas/<id>/raw/`: every read, exactly as received

**Which canvases are read.** A read puts the whole canvas in Claude's context, so unchanged canvases are skipped. Before the reads, Claude searches each channel's files, which shows each canvas's size. The hook keeps only the sizes of the canvases you listed, and drops every other file before anything is saved. Then `diffdata discover slack canvases-due` says which canvases to read. A canvas is read if:
- it has never been read, or it's missing from the search
- its size differs from its size at its last read
- Slackbot has posted "made updates to a canvas tab" about it since its last read
- its last read is older than `canvas_refresh_days` in `config.toml` (7 if you leave it out), because sizes are rounded to 0.1 KB and miss small edits

The first run after updating reads every canvas once, since their earlier reads have no size on record.

Links in a canvas (Docs, images, other canvases) stay links: nothing fetches them, and they're listed in `references/references.md`. Canvas comments aren't collected yet (COL-3).

## Collect Google Docs (COL-4, COL-5)

`diffdata` fetches the docs you choose, with your own Google login, on your own computer. A doc is fetched only if you do both of these:
- **Pick it** in Google's own file picker when you sign in. Your sign-in can reach only the docs you pick, so Google itself enforces this.
- **Opt in to it:** list it in `config.toml`, or approve it at review (see [Approve new canvases and Google files](#approve-new-canvases-and-google-files-con-1)). Either one is your opt-in. Removing it from the list, or withdrawing the approval, stops collection on the next run. A doc that is both listed and approved stays opted in until you remove it and withdraw it.

1. **Get the app's client file** from the project team, and save it as `google-client.json` in your `.diffdata` folder, next to `config.toml` (`C:\Users\<you>\.diffdata\` on Windows). It stays outside the repo. While the app is in testing, the team also has to add your Google account as a test user, and a sign-in lasts 7 days.
2. **List the docs** under `google_docs` in `config.toml`, by ID or by link (`https://docs.google.com/document/d/<ID>/edit`). The same doc listed twice is refused. A doc you approved at review doesn't need listing.
3. **Sign in:** run `uv run diffdata collect google-docs sign-in`. Your browser opens Google's sign-in, then its picker, showing Google Docs only. Pick the docs and allow access. Run it again to pick more. It says how many docs you picked, and how many of those are listed or approved at review.
4. **Fetch:** run `uv run diffdata collect google-docs`. It prints counts only, and never a doc's ID or title. It names a listed doc by its place in your list ("Entry 2"), and an approved doc by its letter in the current withdraw list ("Approved item b"). That letter can change after the next review. It writes, in your data folder:
   - `documents/current/google-doc_<title>_<id>.md`: each doc's latest text
   - `documents/google-doc/<id>/versions/`, `events.jsonl` and `comments.md`: its history, with its comments and suggestions
   - `documents/google-doc/<id>/raw/`: every fetch, exactly as received

Run it again whenever you like. It always reads a doc's comments, but reads its history only if the doc changed. If a doc's whole changelog can't be fetched, its text and comments are still saved, the run says so, and the changelog is tried again next time: until then the doc has no suggestions. If Google refuses one part of a doc for good, such as the text export of a doc too big to export, the rest is saved and the run names the part. `uv run diffdata collect google-docs build` rebuilds from what's saved, without fetching.

**A doc that can't be read** is saved with the reason, and the run carries on: `no access` or `not found` (a listed or approved doc you didn't pick looks like this), `a folder`, or `not a Google Doc`. Folders are refused, and their contents are never listed or opened, because choosing a folder isn't choosing the files in it.

**What the Allow screen means.** Google names the app's developer there, which is the SPARDiffData Google account. That account only registers the app with Google. **It never gets access to anyone's docs.** You sign in with your own account, on your own computer, and the sign-in and the data stay on that computer. Google words the `drive.file` permission as see, edit, create and delete the files you pick, because it has no read-only version. `diffdata` only ever reads them: it sends read requests only, and a test checks that.

**Where the sign-in lives:** `google-token.json`, next to `google-client.json` in your `.diffdata` folder. `diffdata` refuses to keep it inside a git repo or a folder a cloud service syncs. It's sent only to Google. To take away access completely, remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Removing a doc from the list stops collection, but what was already collected stays in your data folder for now.

## Approve new canvases and Google files (CON-1)

The build lists every canvas and Google file your saved Slack data links to. Nothing new is collected until you approve it, **typed in your own terminal**. Claude is stopped from answering for you, and told never to touch the files that hold your choices:

- The command refuses to run anywhere but a terminal a person types in, and this repo's `.claude/settings.json` stops Claude's shell tools from running it.
- The same settings stop Claude's file tools from writing the approvals file or `config.toml`.
- **These checks have limits.** A program written on purpose to fake typing could get past the first. A PowerShell command, or a script that opens the file itself, could still write either file. So `/slack-export` and the repo's instructions for Claude also tell it never to run the command or change those files.

1. **When the build says items are waiting,** open PowerShell or Terminal in the repo folder and run `uv run diffdata consent review`. It explains what approving means, then shows a lettered list: each item's type, its real link, its title, and where and when it was first shared.
2. **Answer** `all`, `none`, or `all but b, d` to skip some. Skipped items aren't asked about again. Pressing Enter alone records nothing, and the items wait for next time.
3. **The next `/slack-export`** reads the approved canvases. An approved Google Doc is fetched by `uv run diffdata collect google-docs`, once you've also picked it at Google's sign-in, the same as one listed in `config.toml`. Other Google files (Sheets, Slides, Drive files) aren't fetched. Attachments and ordinary links are never on the list.

**To withdraw an approval,** run `uv run diffdata consent withdraw` the same way, and answer `all`, `none` or letters such as `b, d`. A canvas stops being read from the next `/slack-export`, and a Google Doc stops being fetched from the next `uv run diffdata collect google-docs`. What was already collected stays in your data folder for now. An item listed in `config.toml` is withdrawn by removing it there: a canvas from `slack_canvases`, a Google Doc from `google_docs`.

Your decisions go in `approvals.jsonl`, next to `config.toml` in `~/.diffdata/`: keys, decisions, times and your answer, never titles. Both commands refuse to write it inside a git repository or a synced folder.

## Scrub your copies (SCR-1, SCR-2)

Run `uv run diffdata scrub` after an export. It makes a scrubbed copy of each opted-in Slack day file, canvas and Google Doc in `scrubbed/` in your data folder. A canvas or Google Doc is opted in if it's listed in `config.toml` or approved at review now: a skipped or withdrawn one gets no copy, and its earlier copy is gone after the scrub. If the approvals file can't be read, only the listed ones get a copy, and the scrub says so. Your originals are only read. Each copy has:

- **secrets** replaced: API keys, tokens, passwords and private keys become `[secret]`, using the rules of gitleaks v8.30.1, run in Python. A password written after its name (`password=…`, `the db password: …`, `my password is …`, `Password is …`) goes however plain it is, and if it has 6 or more characters with a letter and a digit, it goes everywhere it appears in the copy. Text that only looks random, like a key with no known shape, becomes `[secret?]` and is listed in `private/flags.md` by copy, never by its value. Google links keep their document IDs, so they still work, and inside other links a site's name stays readable;
- **meeting links** (Zoom, Google Meet, Teams, Webex, Whereby, Jitsi, Calendly, GoTo) replaced whole, passcode and all, by `[meeting link]`;
- **phone numbers** and `tel:` links replaced by `[phone]`, but not digits inside other links;
- **emails, Slack IDs, mentions and the names in your people list** replaced by `[email]` and `[Person N]`.

**Google Docs** get two copies each: the current version, `documents/current/google-doc_<id>.md`, and its comments, `documents/comments/google-doc_<id>.md` (only if it has comments). The title is scrubbed with the text, and file names hold the ID only. Each comment's author is matched to your people list by their whole Google name: someone in Slack keeps their `[Person N]`, someone new is added to the list with that name and no Slack ID (so their name is then replaced everywhere), and a name two people have becomes `[name]`, flagged. An author with no name shows as `[no name]`, and no Google ID or email is kept. Comments and replies by people in `slack_opted_out` are left out, matched by their whole name; the copy says how many, never whose. Text they typed in the doc itself stays. A doc's versions and suggestions aren't scrubbed or sent yet.

A last check reads each copy again. A copy with anything left is held back: no file is written, and `scrubbed/manifest.json` says what kind of thing was found, never the value. Copies are still marked "not approved for release": names of people who aren't in the people list, and links that open an original (a doc shared with anyone who has the link), aren't checked yet. So read your copies before you share them.

## Upload the scrubbed copies (STO-1 to STO-3)

`uv run diffdata push` sends the copies the last scrub passed to your team's folder in the pilot bucket on Google Cloud Storage. Run it after `uv run diffdata scrub`.

**One-time setup.**
1. **Save the team's key.** It comes in the team file, and `uv run diffdata setup` puts it in place for you as `store-key.json` in `~/.diffdata/`, next to `config.toml`. It can only add files to your team's folder: it can't read, change or delete anything there. Never share it, commit it, or open it in an AI tool. The push refuses a key inside a git repository or a synced folder. There's no Google Cloud CLI to install.
2. **Fill in `[store]` in `config.toml`.** `uv run diffdata setup` does this from the team file. By hand: the team gives your team `bucket`, `folder` and `project`. Only `producer` is yours to choose: a pseudonym that labels your copies in the shared store instead of your name, never your real name. The push refuses to run while any of them is empty.
3. **Decide about copies not approved for release.** Every copy is marked not approved for now: names of people who aren't in the people list aren't checked yet. `uv run diffdata setup` asks whether to send them, with an explicit `y` or `n`, and writes `send_not_approved = "yes"` or `"no"` under `[store]`. With yes, they go to the not-approved part of your team's folder, which is never given to consumers. With no, the push refuses them, so for now it sends nothing.

**Before you push, read your copies.** They're plain text files in `scrubbed/` in your data folder. Look for anything that shouldn't leave your team: a name the scrub missed, a phone number, a password or key, a private meeting link. The scrub replaces phone numbers, keys and meeting links, but no check catches everything, and names of people who aren't in the people list aren't checked yet. You can use an AI tool to help, but it sends what it reads to the company that runs it, so pick one you'd trust with this text. If something is wrong, don't push; tell the project team. Pushing is your OK to store these copies, and you can't remove them from the bucket yourself.

**What it sends, and what it never sends.** Only the copies listed as passed in `scrubbed/manifest.json`. Nothing else in your data folder is sent: not the raw Slack data, not `private/` (the people list and the flags), not `slack/left_out.json`, and not a copy the scrub held back. It stops before sending anything if a scrub is running or was cut short, if a copy changed after the scrub, if the manifest lists anything a scrub doesn't make, or if `scrubbed/` holds a link or a hard-linked file. It also scrubs every copy again, using your people list, and stops if that would change anything, such as a name, a Slack ID, an email, a mention, a secret, a meeting link or a phone number left in it. If it stops for any of these, run the scrub again and push again. It also stops if `slack_opted_out` changed since the copies were made: run the build, then the scrub, then push. And it stops if a copy is of a canvas or Google Doc that isn't listed or approved now, for example one you withdrew after the scrub, or if the approvals file can't be read and the copy's canvas or doc isn't listed: run the scrub again, then push. A copy already in the bucket stays there: ask the project team to remove it. A Google Doc's copies must also be exactly what a scrub makes from the doc now, so if the doc, its comments, your people list or `slack_opted_out` changed since the scrub, it stops: run the scrub again. It checks each canvas or doc again just before sending it, so one withdrawn while a push runs isn't sent. While it runs it holds the scrub's lock, so a scrub can't start until it's done. It refuses to run if `STORAGE_EMULATOR_HOST` or `GOOGLE_CLOUD_UNIVERSE_DOMAIN` is set, since either would send the copies somewhere other than Google.

**It never overwrites.** Each copy goes to `<folder>/<part>/<producer>/<project>/<source type>/<date>/<ID>.<hash>.md`, where `<part>` is `not-approved` (or, once copies can be approved, `approved`) and the hash comes from its content. The same content is skipped, and a changed copy (a day with new messages, say) is added beside the old one. The team's key can only add files, so nothing in the bucket can be replaced or deleted from here.

**A record of each push.** Each push adds a record of what it sent under `pushes/` in the bucket, and keeps the same record on your computer, in `pushes/` in your data folder. It keeps it even when a push stops partway, so you can see what left your computer and compare it with what's stored. The report says where it is.

**Who can read your folder:** the project's admins; you and the other producers on your team, read-only, through your own Google account; and later, consumers you approve (not built yet). Copies not approved for release go to a part of the folder that consumers are never given. To look at what's stored, sign in to Google's web console with your own Google account and open your folder's direct link: `https://console.cloud.google.com/storage/browser/<bucket>/<folder>`, with the bucket and folder from `[store]`. You can see and download the copies there, but not change or delete them. Your folder isn't reachable from the console's list of buckets, and the console shows "Access denied" for the bucket itself and its settings. That's expected: you can read your team's folder only.

## Clear Claude's session history (after collecting)

While `/slack-export` runs, Claude reads whole channels, including messages from people who opted out, and Claude Code keeps those reads in its session history on your computer. When you've finished collecting and pushing, clear it:

1. Close Claude Code (the terminal app and the desktop app).
2. In your own Terminal or PowerShell, in the repo folder where you ran `/slack-export`, run `uv run diffdata clean-history`.
3. It lists each folder of this repo's history it will delete, with its number of sessions. Type `yes` to delete them, or `no` to keep them. Anything else asks again.

`uv run diffdata push` offers this at the end of a push that sent everything: type `yes` to see the list, or press Enter to skip.

It only deletes the folders Claude Code keeps for this repo (and folders inside it, such as worktrees) in `~/.claude/projects/` (or under `CLAUDE_CONFIG_DIR`), and only if every session in a folder was started in this repo. Each folder goes with everything Claude Code keeps in it for this repo, such as its memory notes. If it finds nothing but you have used Claude Code in this repo, tell the project team rather than deleting by hand. It never touches other projects' sessions, Claude's settings, `history.jsonl` (what you typed, in every project), your data folder or `~/.diffdata/`. A folder it isn't sure about is left alone and named. It runs only in a terminal you type in, never from Claude: this repo's settings stop Claude from running it.

## Two hard rules

- We never modify or delete a producer's original files or data. We read originals and only change our own copies.
- Real collected data never goes in a git commit.

## Learn more

- [docs/requirements.md](docs/requirements.md): what we're building, with requirement IDs
- [CONTRIBUTING.md](CONTRIBUTING.md): how to pick up work, step by step, for people new to Git
- [CLAUDE.md](CLAUDE.md): the rules every Claude session follows in this repo
