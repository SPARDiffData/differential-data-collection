# differential-data-collection

`diffdata` finds the working material behind research (Slack threads, canvases, Google Docs, transcripts), collects it, scrubs it and stores it centrally. It's the collection tool for the SPAR Fall 2026 project *Differential Data for Automated AI Safety Research*. We're testing it on our own team's data first.

## Status

**v0.0.1: repo skeleton.** The `diffdata` command and its stages exist. Slack export (DIS-1) works through Claude Code; the other stages are stubs. Next come the reference log, canvas and Google Doc current versions, and pushing to the shared Drive. See [CHANGELOG.md](CHANGELOG.md).

## Quick start

You need [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs Python 3.12 for you.

```
git clone https://github.com/SPARDiffData/differential-data-collection
cd differential-data-collection
uv sync
uv run diffdata --help
uv run pytest
```

To configure it, copy `config.example.toml` to `config.toml` in a `.diffdata` folder in your home folder (`C:\Users\<you>\.diffdata\config.toml` on Windows, `~/.diffdata/config.toml` on a Mac), and `.env.example` to `.env`, then fill them in. Both stay on your computer. Keeping `config.toml` outside the repo means tools working in the repo, such as Codex, can't read it.

## Export a Slack channel (DIS-1)

For the pilot there's no Slack app: Claude Code reads Slack for you through its Slack connector, as you. Nothing writes to Slack. This repo's `.claude/settings.json` removes every Slack write tool from Claude while it works in this folder. During `/slack-export`, a hook also stops any read of a channel or canvas that the export run doesn't name, and any search other than a files or message search of one of the run's channels, before it runs. The run takes its channels and canvases from `config.toml`. If that check can't run, the read is stopped too. The check stays on for the rest of that Claude session, so start a new session for each export, and for any other Slack work afterwards. This needs Claude Code 2.1.295 or later (`claude --version`).

1. **Set up `config.toml`** in `~/.diffdata/`. List the channels to export under `slack_channels`; listing a channel is your opt-in. Set `slack_workspace_url` to your workspace's address. Keep `data_dir` outside the repo and outside OneDrive, Dropbox, Google Drive and iCloud: `diffdata` refuses those.
2. **Try it.** Open Claude Code in the repo folder and type `/slack-export test`. It reads only the 5 newest messages of the first channel, and skips threads.
3. **Run it.** Type `/slack-export`. Claude reads each channel, searches it for late replies, and reads the threads. A hook saves each result unchanged to your data folder, so Claude never retypes it, and then `diffdata` builds:
   - `slack/export/<channel>/<date>.md`: one readable file per day (UTC), with thread replies under their post
   - `slack/store/<channel-id>/messages.jsonl`: every version of every message, for the later steps

Claude reports counts only. The raw results, including authors' emails, stay in your data folder until scrubbing (SCR-2).

By hand: `uv run diffdata discover slack build` rebuilds the export from what's saved, and `uv run diffdata discover slack abort` stops a run. A run also closes on its own after an hour. If Claude says `uv` isn't found, restart VS Code.

**Only what's new.** The first complete export reads each channel from the start. After that, each run reads a channel only from an hour before the last complete run began. The hour covers clock differences and delays in Slack's search. A message search of the same channel finds new replies in older threads, and Claude then reads each of those threads again in full. The search's results aren't saved, only the threads it found replies in. A channel's start point moves forward only when a run read every page, finished the search, and read every thread listed. Otherwise the build says so, in counts, and the next run reads the same stretch again, so a skipped page can't leave a gap. A test run skips most of that, so it leaves the start point where it was. Each channel's start point is in `slack/state.json` in your data folder: delete that file and the next run reads everything again.

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

Links in a canvas (Docs, images, other canvases) stay links: nothing fetches them. Canvas comments aren't collected yet (COL-3).

## Two hard rules

- We never modify or delete a producer's original files or data. We read originals and only change our own copies.
- Real collected data never goes in a git commit.

## Learn more

- [docs/requirements.md](docs/requirements.md): what we're building, with requirement IDs
- [CONTRIBUTING.md](CONTRIBUTING.md): how to pick up work, step by step, for people new to Git
- [CLAUDE.md](CLAUDE.md): the rules every Claude session follows in this repo
