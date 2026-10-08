# Contributing

This is how we work together. You don't need to know Git well: Claude does most of the typing, and this page tells you what to ask it for. If you get stuck at any step, ask in the team Slack channel or ask Paul.

## Two hard rules

- **Never modify or delete a producer's original files or data**, on your computer or in any cloud service. We only read originals, and only ever change our own copies.
- **Real collected data never goes in a git commit**, not even on a branch or "just for a test". Collected data stays in your data folder, outside the repo. Tests use made-up data only.

If you think real data, a password or a token got into a commit or a PR, don't try to fix it quietly. Tell Paul straight away. The repo is public, so it's exposed as soon as it's pushed, and a leaked token must be revoked.

## One-time setup

1. **Install the tools:**
   - Git: [git-scm.com/downloads](https://git-scm.com/downloads). On a Mac you can instead run `git --version` in Terminal and accept the install prompt.
   - uv, which manages Python for us: [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/).
   - GitHub CLI: [cli.github.com](https://cli.github.com).
   - Claude Code.
2. **Tell Git who you are.** Use your GitHub no-reply email so your personal address stays private. Find it on GitHub under Settings → Emails; it looks like `12345678+username@users.noreply.github.com`.
   ```
   git config --global user.name "Your Name"
   git config --global user.email "12345678+username@users.noreply.github.com"
   ```
3. **Log in to GitHub** from a terminal: `gh auth login`. Choose GitHub.com, then HTTPS, then log in with a web browser.
4. **Get the code:**
   ```
   gh repo clone SPARDiffData/differential-data-collection
   cd differential-data-collection
   uv sync
   ```
5. **Make your private settings files.** Copy `config.example.toml` to `config.toml` and `.env.example` to `.env`, then fill them in. Both copies stay on your computer and are never committed. Keep `data_dir` pointing outside the repo; the tool refuses a folder inside it.

You can also open Claude Code in an empty folder and say: "Walk me through the one-time setup in CONTRIBUTING.md of SPARDiffData/differential-data-collection."

## Each piece of work

1. **Claim an Issue.** Open it on GitHub and assign yourself (right-hand side, "Assignees"). If there's no Issue for what you want to do, ask Paul to open one.
2. **Start a branch from the latest `main`.** Branches are named `<lane>/<short-name>`, for example `discover/slack-export`. Tell Claude: *"Pull main and make a branch called discover/slack-export."*
3. **Work with Claude.** Start with: *"Read Issue #12 and propose a plan."* Claude reads `CLAUDE.md` for our rules. Check the plan before saying go.
4. **Run the tests:** *"Run the tests and the linter."* (That's `uv run pytest` and `uv run ruff check`.)
5. **Push and open a PR:** *"Commit, push and open a PR with the template."* Check it says `Closes #12` and lists the requirement IDs.
6. **Get a review.** Open a new, separate Claude session in the repo (a fresh terminal window, not the one that wrote the code) and run `/review-pr 34` with your PR number. It writes a short review split into must-fix and nice-to-have, and posts it on the PR when you say yes. Fix the must-fix items, push, and run it again. If your PR touches consent, scrub or store, a human must review it too: ask the lane lead or Paul.
7. **Merge.** When CI is green and the review is done, click **Squash and merge** on the PR page. GitHub deletes the branch for you.
8. **Tidy up:** *"Switch back to main and pull."*

## When `main` moved under you

If someone else merged first, GitHub may say your branch is out of date or has conflicts. Ask Claude: *"Bring my branch up to date with main."* It merges the latest `main` into your branch and walks you through any conflicts. If a conflict is in someone else's lane, ask them before choosing which version wins.

## Lanes

Each lane has its own package, so we can work in parallel without blocking each other. Stay in your lane's folder unless the Issue says otherwise.

Find your lane from the requirement IDs in your Issue.

| Lane | Folder | Requirements | Lead | Human review |
|---|---|---|---|---|
| consent | `src/diffdata/consent/` | CON, REV | Paul | required |
| discover | `src/diffdata/discover/` | DIS | Paul | |
| collect | `src/diffdata/collect/` | COL | Paul (core), Lachlan (source detail) | |
| organize | `src/diffdata/organize/` | ORG, HIS | Lachlan | |
| scrub | `src/diffdata/scrub/` | SCR | Ananjay | required |
| store | `src/diffdata/store/` | STO, REC | not assigned yet | required |
| common | `src/diffdata/common/` | COL-10, ORG-2 | shared | |
| repo | docs, CI, tooling | ENG | Paul | |

## Review policy

Every PR gets one review before merge. An AI review from a separate Claude session (`/review-pr`) is enough for most code. A human reviews anything touching consent, scrub or store.

## Getting help

- Stuck with Git or Claude: ask in the team Slack channel, or ask Paul.
- Unsure what an Issue means: comment on the Issue.
- Unsure whether something counts as real data: assume it does, and ask before committing.
