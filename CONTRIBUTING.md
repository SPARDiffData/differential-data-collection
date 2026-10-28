# Contributing

This is how we work together. You don't need to know Git well: Claude does most of the typing, and this page tells you what to ask it for. If you get stuck at any step, ask in the team Slack channel or ask Paul.

**Team only, for now.** The repo is public so anyone can read it, but only the SPAR project team contributes. Pull requests from outside the team are closed without review.

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
4. **Get access to the repo.** Ask Paul to add you as a collaborator, then accept the invitation (it arrives by email and at github.com/notifications). You need it to push your branches. If Git, GitHub or Claude ever offers to make a fork, say no: a PR from a fork counts as outside the team, and `/review-pr` won't review it.
5. **Get the code:**
   ```
   gh repo clone SPARDiffData/differential-data-collection
   cd differential-data-collection
   uv sync
   ```
6. **Make your private settings files.** Make a folder called `.diffdata` in your home folder (`C:\Users\<you>\.diffdata` on Windows, `~/.diffdata` on a Mac). Copy `config.example.toml` into it as `config.toml`, and copy `.env.example` to `.env` in the repo folder, then fill them in. Keeping `config.toml` outside the repo means tools working in the repo, such as Codex, can't read it. Both copies stay on your computer and are never committed. Keep `data_dir` pointing outside the repo; the tool refuses a folder inside it.

You can also open Claude Code in an empty folder and say: "Walk me through the one-time setup in CONTRIBUTING.md of SPARDiffData/differential-data-collection."

## Each piece of work

1. **Claim an Issue.** Open it on GitHub and assign yourself (right-hand side, "Assignees"). If there's no Issue for what you want to do, ask Paul to open one.
2. **Start a branch from the latest `main`.** Branches are named `<lane>/<short-name>`, for example `discover/slack-export`. Tell Claude: *"Pull main and make a branch called discover/slack-export."*
3. **Work with Claude.** Start with: *"Read Issue #12 and propose a plan."* Claude reads `CLAUDE.md` for our rules. Check the plan before saying go. If you'd like a more structured plan, run `/plan-issue 12` instead. Claude asks its open questions first, then writes the plan in plan mode, and once you approve, posts it on the Issue so it isn't lost when the session ends. It's optional: plan whichever way suits you.
4. **Run the tests:** *"Run the tests and the linter."* (That's `uv run pytest` and `uv run ruff check`.)
5. **Push and open a PR:** *"Commit, push and open a PR with the template."* Check it says `Closes #12` and lists the requirement IDs.
6. **Get a review.** Tell Claude: *"Review PR #34 with the pr-reviewer agent, and post the review."* The agent runs `/review-pr` in its own context: it doesn't see your conversation, and it has no file-editing tools. Claude posts only the review; the agent's last lines are notes for Claude, not for the PR. Or open a new, separate Claude session in the repo and run `/review-pr 34` there; it posts when you say yes. Either way you get a short review split into must-fix and nice-to-have. Fix the must-fix items, push, and run it again. Then ask Paul to review it: on the PR page, click **Reviewers** on the right and choose Paul. See [Review policy](#review-policy). To check a PR quickly, a reviewer can also run `/debrief 34`. It makes a one-page summary, as a private claude.ai page, of what was asked, what was delivered and which decisions need a look. It's optional and doesn't replace `/review-pr`.
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

Every PR gets reviewed before merge:

- **`/review-pr` first, on every PR.** It's a quick, independent check that catches must-fix items before anyone else spends time on them. It runs in its own context: either as the `pr-reviewer` agent (`.claude/agents/pr-reviewer.md`), which takes only the PR number and has no file-editing tools, or in a separate Claude session.
- **Paul reviews teammates' PRs.** His Claude session runs the same AI reviews and posts them on the PR. It never pushes to a teammate's branch: the author makes the fixes, and decisions go to Paul.
- **A human reviews anything touching consent, scrub or store.** For a teammate's PR, that's Paul. For Paul's own, see [Human review for consent, scrub and store](#human-review-for-consent-scrub-and-store).
- **If Paul becomes a bottleneck,** we'll change this. Any human can review, whichever AI they use.

### Codex, for risky changes

Codex, OpenAI's coding agent, gives a second AI review, from a different company. Two models catch more than one. It runs on Paul's machine with a paid API key, so it's used only where it's worth the cost:
- code under `src/` that touches consent, scrub or store, or a stored format
- a hook or permission in `.claude/`
- large changes

Whether it runs is agreed when the work starts: Claude says whether it's worth it, and Paul confirms along with the plan.

Codex runs after `/review-pr`, once that review's must-fix items are fixed: on the same commit if there are none, otherwise on the fix commit. That way it doesn't spend a paid run reporting what's already being fixed, and it sees the fixes, which is where new bugs tend to hide.

Codex runs on a separate worktree of the PR branch, so nobody's folder has to switch branches, and at most twice per PR. It never gets the data folder, `config.toml` or `.env`, because it sends what it reads to OpenAI. `.env` moves out of the repo folder, to `~/.diffdata/`, before it holds a real token (Issue #45). Teammates without Codex skip it.

### How Claude runs the reviews on Paul's PRs

Since 2026-10-16, the Claude session that wrote the code runs the reviews itself, so Paul doesn't carry results between sessions:

1. Once the PR is open and CI passes, it runs the `pr-reviewer` agent. It posts the review on the PR word for word, leaving out the agent's notes after the review: its Codex focus line and its list of denied tool calls.
2. If Codex was agreed, it runs next, with that focus line, and its output is posted word for word too. With nothing under Must fix, Codex runs on the same commit. Otherwise the must-fix items are fixed first, and Codex runs on the fix commit, at the same time as step 4's `/review-pr`.
3. It fixes the must-fix items, Codex's high findings, and small items inside the PR's own lines. Everything else goes into one follow-up Issue for the PR. After each round of fixes, a "Review fixes" comment lists what was fixed, what was backlogged, and what wasn't changed and why.
4. It runs `/review-pr` once more on the fix commit. Codex runs at most twice per PR. If must-fix items are still open after that, it stops and asks Paul. The commit Paul merges always gets a final `/review-pr`, even when fixes land after the last round: the limit is on fix rounds and on Codex, not on checking what gets merged.
5. It gives Paul one line: the verdict, what was fixed and backlogged, and what needs him. Paul does the human checks the PR needs (see [Human review for consent, scrub and store](#human-review-for-consent-scrub-and-store)), then merges.

### Human review for consent, scrub and store

These lanes decide what we collect, what we remove before release and where data goes, so a mistake there costs people's trust. For Paul's own PRs in these lanes, each person checks what they check best:

1. **Before coding, the rules in plain words.** The plan lists what the code must refuse or stop on, and what happens instead: its must-refuse list. Paul approves it along with the plan. `/plan-issue` asks for it.
2. **Before merge, a hands-on check.** Paul runs the change himself, or watches a demo, and sees each refusal happen.
3. **A teammate who reads Python reads the code.** That's Lachlan, who leads legal and compliance.
4. **Both AI reviews,** `/review-pr` and Codex, with Codex always run.

The PR says it got this review by ticking the template's box for it.

**How we got here.** From 2026-10-09 to 2026-10-16, Paul worked solo, and the AI review plus his own read counted as the human review. Until 2026-10-23, Paul read every changed line himself. His hands-on runs caught more than those reads did: on #59, a real export run found a bug that every AI review had missed.

## Getting help

- Stuck with Git or Claude: First try asking your current Claude session.  If that doesn't work, start a new Claude session, explain you're learning, and need help.  If that still doesn't resolve ask in the team Slack channel, or ask Paul. A tip: When Paul ran his first Claude Code work, he kept 2 sessions going at once.  The actual code development, and another acting as a trusted mentor that could answer questions and give advice.
- Unsure what an Issue means: comment on the Issue.
- Unsure whether something counts as real data: assume it does, and ask before committing.
