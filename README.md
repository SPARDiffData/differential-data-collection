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

To configure it, copy `config.example.toml` to `config.toml` and `.env.example` to `.env`, then fill them in. Both stay on your computer.

## Export a Slack channel (DIS-1)

For the pilot there's no Slack app: Claude Code reads Slack for you through its Slack connector, as you. Nothing writes to Slack. This repo's `.claude/settings.json` removes every Slack write tool from Claude while it works in this folder.

1. **Set up `config.toml`.** List the channels to export under `slack_channels`; listing a channel is your opt-in. Set `slack_workspace_url` to your workspace's address. Keep `data_dir` outside the repo and outside OneDrive, Dropbox, Google Drive and iCloud: `diffdata` refuses those.
2. **Try it.** Open Claude Code in the repo folder and type `/slack-export test`. It reads only the 5 newest messages of the first channel.
3. **Run it.** Type `/slack-export`. Claude reads each channel and its threads. A hook saves each result unchanged to your data folder, so Claude never retypes it, and then `diffdata` builds:
   - `slack/export/<channel>/<date>.md`: one readable file per day (UTC), with thread replies under their post
   - `slack/store/<channel-id>/messages.jsonl`: every version of every message, for the later steps

Claude reports counts only. The raw results, including authors' emails, stay in your data folder until scrubbing (SCR-2).

By hand: `uv run diffdata discover slack build` rebuilds the export from what's saved, and `uv run diffdata discover slack abort` stops a run. A run also closes on its own after an hour. If Claude says `uv` isn't found, restart VS Code.

Not yet: each run reads every channel from the start. Reading only what's new, including late replies in older threads, comes next.

## Two hard rules

- We never modify or delete a producer's original files or data. We read originals and only change our own copies.
- Real collected data never goes in a git commit.

## Learn more

- [docs/requirements.md](docs/requirements.md): what we're building, with requirement IDs
- [CONTRIBUTING.md](CONTRIBUTING.md): how to pick up work, step by step, for people new to Git
- [CLAUDE.md](CLAUDE.md): the rules every Claude session follows in this repo
