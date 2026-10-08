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

<!-- Every PR gets one review before merge. Run /review-pr <number> in a separate Claude session.
     A human must also review anything touching consent, scrub or store. -->

- [ ] AI review from a separate Claude session, posted as a comment below
- [ ] Touches consent, scrub or store, so a human reviews it too: @

## Checklist

- [ ] No real data, tokens or `.env` in this PR (test fixtures are synthetic)
- [ ] Never modifies or deletes a producer's original files or data
- [ ] Tests added or updated, and `uv run pytest` passes
- [ ] Stays inside this lane's package, or the Issue says otherwise
