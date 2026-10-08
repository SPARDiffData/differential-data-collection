# differential-data-collection

`diffdata` finds the working material behind research (Slack threads, canvases, Google Docs, transcripts), collects it, scrubs it and stores it centrally. It's the collection tool for the SPAR Fall 2026 project *Differential Data for Automated AI Safety Research*. We're testing it on our own team's data first.

## Status

**v0.0.1: repo skeleton.** The `diffdata` command and its stages exist, but nothing is collected yet. The first feature Issues come next: Slack export, the reference log, canvas and Google Doc current versions, and pushing to the shared Drive. See [CHANGELOG.md](CHANGELOG.md).

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

## Two hard rules

- We never modify or delete a producer's original files or data. We read originals and only change our own copies.
- Real collected data never goes in a git commit.

## Learn more

- [docs/requirements.md](docs/requirements.md): what we're building, with requirement IDs
- [CONTRIBUTING.md](CONTRIBUTING.md): how to pick up work, step by step, for people new to Git
- [CLAUDE.md](CLAUDE.md): the rules every Claude session follows in this repo
