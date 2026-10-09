# CLAUDE.md

Context for every Claude session in this repo. Read it before you start.

## What this is

`diffdata` is the collection tool for the SPAR Fall 2026 project *Differential Data for Automated AI Safety Research*. It finds the working material behind research (Slack threads, canvases, Google Docs, transcripts), collects it, scrubs it and stores it centrally. We dogfood on our own SPAR team's data first.

The requirements, with the IDs used in every Issue and PR, are in [docs/requirements.md](docs/requirements.md). Read the sections for the IDs you're working on.

Most of the team is new to Git and builds by working with Claude. Explain what you're doing in plain words, and prefer the simple way.

## Hard rules

1. **Never modify or delete a producer's original files or data**, locally or in any cloud service. We read, and we only ever change our own copies. Request read-only access scopes wherever a service offers them.
2. **Real collected data never goes in a git commit.** That includes branches that never merge, "just for a test", and trimmed or anonymized samples. Local data lives in the `data_dir` from `config.toml`, which must be outside any git repository. Get it with `diffdata.common.paths.data_dir()`, which refuses a folder inside a repo. Tests use synthetic fixtures only (`tests/fixtures/`).
3. **No secrets in the repo.** Tokens and keys live in `.env`, which is gitignored. `.env.example` holds names only, never values.

If a change would break one of these, or you aren't sure, stop and ask the user. If real data or a secret was already pushed, tell the user to alert Paul right away. The repo is public, so it's exposed even if the PR never merges, and a secret must be revoked.

## Rules for Claude

- Always work on a branch named `<lane>/<short-name>` (for example `discover/slack-export`). Never commit to `main` or push to it.
- Stay inside the lane's package and its tests unless the Issue says otherwise.
- Link the Issue (`Closes #N`) and the requirement IDs in every PR.
- Ask before adding a dependency (anything new in `pyproject.toml`). Prefer the standard library, then a well-known library over writing our own (requirements §2, "Don't reinvent the wheel").
- Add or update tests with every change in behavior. Before pushing, run `uv run ruff format`, `uv run ruff check` and `uv run pytest`.
- Write PR descriptions a non-coder can follow, using the PR template.
- Don't change `docs/requirements.md` unless the Issue asks. Requirement IDs never change once used.

## Lanes

| Lane | Where | Requirements | Human review |
|---|---|---|---|
| consent | `src/diffdata/consent/` | CON, REV (the producer's review before release) | required |
| discover | `src/diffdata/discover/` | DIS | |
| collect | `src/diffdata/collect/` | COL | |
| organize | `src/diffdata/organize/` | ORG, HIS | |
| scrub | `src/diffdata/scrub/` | SCR | required |
| store | `src/diffdata/store/` | STO, REC | required |
| common | `src/diffdata/common/` | shared tags and format (COL-10, ORG-2) | |
| repo | docs, CI, tooling, top-level files | ENG | |

## Repo map

```
src/diffdata/
  cli.py            the `diffdata` command: discover, collect, scrub, push (stubs for now)
  consent/ discover/ collect/ organize/ scrub/ store/    one package per stage
  common/           shared pieces; paths.py holds the data folder guard
tests/              pytest; fixtures/ holds synthetic data only
docs/requirements.md
config.example.toml copy to config.toml (gitignored) and fill in
.env.example        copy to .env (gitignored) and fill in
.claude/commands/review-pr.md   /review-pr, the independent PR review
.github/            PR template, Issue template, CI workflow
```

## Run and test

```
uv sync                  # set up the environment (installs Python 3.12 if needed)
uv run diffdata --help
uv run pytest
uv run ruff check        # lint
uv run ruff format       # format
```

CI runs lint and tests on Windows, macOS and Linux for every PR.

## Reviews and versions

Every PR gets one review before merge: `/review-pr <number>`, run in a separate Claude session. A human must also review anything touching consent, scrub or store. While Paul works solo, Paul's own read is that human review, and the PR ticks the template's "Solo phase" box. The full workflow, including when the solo phase ends, is in [CONTRIBUTING.md](CONTRIBUTING.md).

The version lives in `pyproject.toml` only (0.MINOR.PATCH, matching the phases in the requirements). Paul tags releases and keeps [CHANGELOG.md](CHANGELOG.md).
