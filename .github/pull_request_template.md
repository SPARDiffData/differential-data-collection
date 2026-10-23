<!-- Fill in each section in plain words: a teammate who doesn't code should be able to follow it. -->

## What

<!-- What does this PR change? One or two sentences. -->

## Why

<!-- What problem does it solve, or what does it make possible? -->

## Issue

Closes #

## Requirement IDs

<!-- From docs/requirements.md, e.g. DIS-1, COL-7. Write "none" for repo chores. -->

## How it was tested

<!-- The commands you ran (e.g. `uv run pytest`) and anything you checked by hand. -->

## Reviewer

<!-- Every PR gets /review-pr before merge: ask Claude to run the pr-reviewer agent and post
     the review, or run /review-pr <number> in a separate Claude session.
     A human must also review anything touching consent, scrub or store.
     See "Review policy" in CONTRIBUTING.md. -->

- [ ] `/review-pr`'s review is posted as a comment below
- [ ] Touches consent, scrub or store, so a human reviews it too: @
- [ ] Paul's own consent, scrub or store PR: Paul approved the plan's must-refuse list and checks it by hand, Lachlan reads the code, and Codex's review is posted too

## Checklist

- [ ] No real data, tokens or `.env` in this PR (test fixtures are synthetic)
- [ ] Never modifies or deletes a producer's original files or data
- [ ] Tests added or updated, and `uv run pytest` passes
- [ ] Stays inside this lane's package, or the Issue says otherwise
