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
6. **Make your private settings files.** Run `uv run diffdata setup <team file>` with the team file the project team sent you, in your own Terminal or PowerShell, in the repo folder. It asks for your channels, shows what it will write, and writes `config.toml` in a `.diffdata` folder in your home folder (`C:\Users\<you>\.diffdata` on Windows, `~/.diffdata` on a Mac). Your producer handle is a pseudonym: it labels your copies in the shared store instead of your name. Or make that folder yourself and copy `config.example.toml` into it as `config.toml`. Either way, copy `.env.example` there as `.env`, then fill it in. Keeping them outside the repo means tools working in the repo, such as Codex, can't read them. Both copies stay on your computer and are never committed. If you made a `.env` in the repo folder before, move it there: the tests fail while it's in the repo folder. Keep `data_dir` pointing outside the repo; the tool refuses a folder inside it.

You can also open Claude Code in an empty folder and say: "Walk me through the one-time setup in CONTRIBUTING.md of SPARDiffData/differential-data-collection."

## Each piece of work

1. **Claim an Issue.** Open it on GitHub and assign yourself (right-hand side, "Assignees"). If there's no Issue for what you want to do, open one (Claude's `/plan-issue` can help), and name it in that day's update to Paul.
2. **Start a branch from the latest `main`.** Branches are named `<lane>/<short-name>`, for example `discover/slack-export`. Tell Claude: *"Pull main and make a branch called discover/slack-export."*
3. **Work with Claude.** Start with: *"Read Issue #12 and propose a plan."* Claude reads `CLAUDE.md` for our rules. Check the plan before saying go. If you'd like a more structured plan, run `/plan-issue 12` instead. Claude asks its open questions first, then writes the plan in plan mode, and once you approve, posts it on the Issue so it isn't lost when the session ends. It's optional: plan whichever way suits you.
4. **Run the tests:** *"Run the tests and the linter."* (That's `uv run pytest` and `uv run ruff check`.)
5. **Push and open a PR:** *"Commit, push and open a PR with the template."* Check it says `Closes #12` and lists the requirement IDs.
6. **Get a review.** Tell Claude: *"Review PR #34 with the pr-reviewer agent, and post the review."* The agent runs `/review-pr` in its own context: it doesn't see your conversation, and it has no file-editing tools. Claude posts only the review; the agent's last lines are notes for Claude, not for the PR. Or open a new, separate Claude session in the repo and run `/review-pr 34` there; it posts when you say yes. Either way you get a short review split into must-fix and nice-to-have. Fix the must-fix items, push, and run it again. If you have Codex, run its review too. You don't need to wait for Paul: see [Review policy](#review-policy). To check a PR quickly, a reviewer can also run `/debrief 34`. It makes a one-page summary, as a private claude.ai page, of what was asked, what was delivered and which decisions need a look. It's optional and doesn't replace `/review-pr`.
7. **Merge.** When CI is green and the reviews are done, click **Squash and merge** on the PR page. GitHub deletes the branch for you. Put the PR in that day's update to Paul (see [Review policy](#review-policy)).
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

Since 2026-11-23, PRs don't wait for Paul. Lachlan works on the project full time, and waiting for Paul's approval made him a bottleneck, so Paul asked for this change. Paul checks the work after it merges instead, from a daily update.

Every PR gets reviewed before merge:

- **`/review-pr` first, on every PR.** It's a quick, independent check that catches must-fix items early. It runs in its own context: either as the `pr-reviewer` agent (`.claude/agents/pr-reviewer.md`), which takes only the PR number and has no file-editing tools, or in a separate Claude session.
- **A Codex review on top, recommended.** See [Codex, a second AI review](#codex-a-second-ai-review).
- **The author reviews and merges.** When CI is green, `/review-pr` has nothing under Must fix, and Codex's high findings are fixed or answered, the author merges. A teammate's review is welcome but not required. No PR waits for Paul's approval.
- **Consent, scrub and store get human checks too.** The author does them: see [Human review for consent, scrub and store](#human-review-for-consent-scrub-and-store).
- **Claude never pushes to a teammate's branch.** The author makes the fixes on their own PR.

### The daily update to Paul

At the end of each working day, whoever merged PRs sends Paul a short update in the team Slack channel. Claude can draft it. It lists:

- each PR merged that day: what it changes, in a line or two
- decisions made without Paul, and why
- anything that may need fixing, or that Paul should check

Paul reads it when he can and asks for changes where he thinks they're needed. Each request becomes an Issue, or a fix on the PR if it hasn't merged yet. If Paul hasn't seen something yet, that doesn't block the next step.

### Codex, a second AI review

Codex, OpenAI's coding agent, gives a second AI review, from a different company. It catches things Claude misses, so we recommend it on top of `/review-pr`, especially for:
- code under `src/` that touches consent, scrub or store, or a stored format
- a hook or permission in `.claude/`
- large changes

It's a recommendation, not a requirement: whoever has Codex runs it, and anyone without it skips it. If Codex is skipped on one of the changes above, say so in the PR.

Codex runs after `/review-pr`, once that review's must-fix items are fixed: on the same commit if there are none, otherwise on the fix commit. That way it doesn't report what's already being fixed, and it sees the fixes, which is where new bugs tend to hide.

Codex runs on a separate worktree of the PR branch, so nobody's folder has to switch branches, and at most twice per PR. It never gets the data folder, `config.toml` or `.env`, because it sends what it reads to OpenAI. That's why `config.toml` and `.env` live in `~/.diffdata/`, outside the repo folder.

### How Claude runs the reviews

The Claude session that wrote the code runs the reviews itself, so nobody carries results between sessions:

1. Once the PR is open and CI passes, it runs the `pr-reviewer` agent. It posts the review on the PR word for word, leaving out the agent's notes after the review: its Codex focus line and its list of denied tool calls.
2. If the user has Codex and the review has nothing under Must fix, Codex runs next, on the same commit, with that focus line. Its output is posted word for word too.
3. It fixes the must-fix items, Codex's high findings, and small items inside the PR's own lines. Everything else goes into one follow-up Issue for the PR. After each round of fixes, a "Review fixes" comment lists what was fixed, what was backlogged, and what wasn't changed and why.
4. It runs `/review-pr` once more on the fix commit. If Codex hasn't run yet, because the first review had must-fix items, Codex runs on the fix commit at the same time. Codex's findings from that run, and the re-review's nice-to-haves, are handled as in step 3, with their own "Review fixes" comment. If the re-review itself still has must-fix items, it stops and asks the user instead of fixing them. Codex runs at most twice per PR. The commit that merges always gets a final `/review-pr`, even when fixes land after the last round: the limit is on fix rounds and on Codex, not on checking what gets merged.
5. It gives the user one line: the verdict, what was fixed and backlogged, and what needs them. The user does the human checks the PR needs (see below), merges, and adds the PR to the daily update.

### Human review for consent, scrub and store

These lanes decide what we collect, what we remove before release and where data goes, so a mistake there costs people's trust. The person who opens the PR does these checks, whoever they are:

1. **Before coding, the rules in plain words.** The plan lists what the code must refuse or stop on, and what happens instead: its must-refuse list. The author approves it along with the plan, and it goes on the Issue. `/plan-issue` asks for it.
2. **Before merge, a hands-on check.** The author runs the change, or watches a demo, and sees each refusal happen. Each item on the list also has a test.
3. **The author reads the code.** A teammate's read is welcome but not required.
4. **Both AI reviews:** `/review-pr`, and Codex if the author has it.

The PR says it got this review by ticking the template's box for it. The daily update names it, so Paul can check the must-refuse list after the fact.

**How we got here.** From 2026-10-09 to 2026-10-16, Paul worked solo, and the AI review plus his own read counted as the human review. Until 2026-10-23, Paul read every changed line himself. His hands-on runs caught more than those reads did: on #59, a real export run found a bug that every AI review had missed. Until 2026-11-23, Paul reviewed every teammate's PR, and ran Codex for the team. From 2026-11-23, authors review and merge their own PRs, Paul gets a daily update, and Codex is recommended for anyone who has it.

## For the project team

**Setting up a pilot team** (#132). You need the Google Cloud CLI (`gcloud`): `curl https://sdk.cloud.google.com | bash`, then open a new terminal (see [Google's install page](https://cloud.google.com/sdk/docs/install) for other ways). Producers never need it. Run this yourself, in your own terminal on macOS or Linux, signed in to `gcloud` as the Cloud project's owner account (`gcloud auth login`). Claude's tools may not run it.

```bash
uv run diffdata admin add-team team-a --bucket example-bucket --project example-project --gcp-project example-gcp-project --producer producer@example.com --google-client ~/Downloads/client.json --optout-form "https://…/viewform?usp=pp_url&entry.123="
```

- `team-a` is the team's folder name: letters, digits, `-` and `_`. Repeat `--producer` for each producer's Google account.
- It looks first and changes nothing until you type y. It shows what it will make: an upload account for the team, the managed folder `team-a/` with `approved/`, `not-approved/` and `_config/` inside it, and these grants only:
  - the upload account: *Storage Object Creator* on `approved/` and `not-approved/`, and *Storage Object Viewer* on `_config/`;
  - each producer: *Storage Object Viewer* on `team-a/`.
- It never changes the project's or the bucket's own access. It stops if the folder, or a folder inside it, already has someone it wouldn't add, if the team's folder holds any other managed folder, or if the upload account already has a key. It looks again after your y.
- It writes `~/diffdata-teams/team-a.diffdata-team.json` (choose another place with `--out`, outside any repository or synced folder), readable only by you, and never replaces one. It never prints the key. Send the file to the producer privately: never in the repo, an Issue, a PR or a channel. Delete your copy once they've run `diffdata setup`.
- `--optout-form` is the shared opt-out form's pre-filled link, cut just after the team field's `=`. It prints the team's own link, and the channel notice with the link in it, for the producer to post. The notice wording is a placeholder until it's agreed.
- If it stops partway, it lists what it changed. Run it again with the same values to finish: it skips what's there. If the team file can't be written, it deletes the key it just made, so a second run can finish.

**The opt-out list on day 7** (#132, decision 6). The producer's export won't start until 7 days after a channel's notice, and then only once the team's list is in its folder. When the 7 days are over, look at the form's answers for the team, find each person's Slack user ID (`U…`), and run, in your own terminal:

```bash
uv run diffdata admin optouts team-a --bucket example-bucket
```

- Paste the IDs when it asks, one per line, then an empty line. Type `none` if nobody opted out. IDs never go on the command line, so they stay out of your shell's history.
- It shows the team, how many are on the list already, and each ID it will add, and writes nothing until you type y. It writes `team-a/_config/opted-out.json`, which the team's upload key, its producers and the project's admins can read (the producers' read access to the team's folder covers `_config/` too).
- A list that's there already is only added to, never shortened. If someone opts out late, run it again with their ID. If the list changed while you typed, it stops: run it again.
- Write it only after every channel's 7 days are over: the producer's export refuses a list written before then, since the last answers may be missing from it.

## Getting help

- Stuck with Git or Claude: First try asking your current Claude session.  If that doesn't work, start a new Claude session, explain you're learning, and need help.  If that still doesn't resolve ask in the team Slack channel, or ask Paul. A tip: When Paul ran his first Claude Code work, he kept 2 sessions going at once.  The actual code development, and another acting as a trusted mentor that could answer questions and give advice.
- Unsure what an Issue means: comment on the Issue.
- Unsure whether something counts as real data: assume it does, and ask before committing.
