---
description: Independent review of a pull request against its Issue and our rules. Run it in a separate Claude session from the one that wrote the code.
argument-hint: <PR number>
allowed-tools: Bash(gh pr view:*), Bash(gh pr diff:*), Bash(gh pr checks:*), Bash(gh issue view:*), Bash(gh auth status:*), Bash(git fetch:*), Bash(git diff:*), Bash(git log:*), Read, Grep, Glob
---

Review pull request #$ARGUMENTS in this repo. If no number was given, review the PR for the current branch (`gh pr view` with no number).

You are the independent check: run in a different session from the one that wrote the code, and judge the change on its own merits. **Do not edit files, commit or push.** Only report.

## 1. Gather

- `gh pr view $ARGUMENTS --json number,title,body,author,headRefName,headRefOid,files,closingIssuesReferences`
- `gh pr diff $ARGUMENTS`
- `gh issue view <n>` for the Issue the PR closes (from `closingIssuesReferences`, or the `#n` in the body)
- `gh pr checks $ARGUMENTS` for CI
- `CLAUDE.md`, and each requirement ID the PR names in `docs/requirements.md`
- The "While Paul works solo" section of `CONTRIBUTING.md`

If `gh` is missing or not logged in (`gh auth status`), tell the user, then fall back to `git fetch origin` and `git diff origin/main...HEAD` on the checked-out branch, and ask them to paste the Issue text.

## 2. Check

1. **Does what the Issue asks.** No more, no less. Are the requirement IDs listed, and are they the right ones?
2. **Never modify originals (hard rule).** Look for any code that writes, edits, moves or deletes a producer's files or cloud data, or that requests anything broader than read-only access (for example a Google scope without `.readonly`, or Slack write scopes). Writing is only allowed to our own copies, in the data folder from `diffdata.common.paths.data_dir()`.
3. **No real data or secrets (hard rule).** Every changed file, including fixtures, docs and notebooks: real names, emails, message text, document content, tokens, keys, `.env`, `config.toml`, or files under `data/`, `output/` or `exports/`. Fixtures must be obviously synthetic. If you find something real, put it first under Must fix and tell the user directly: the repo is public, so it's already exposed once pushed. A real secret must be revoked now, and Paul must be told today.
4. **Tests.** Is changed behavior covered by new or updated tests? Do the tests use only synthetic fixtures?
5. **Lane.** The branch should be `<lane>/<short-name>`, and the changed files should stay inside `src/diffdata/<lane>/` and its tests, unless the Issue says otherwise. Lane `repo` covers docs, CI and tooling.
6. **Human review needed?** If the diff touches `src/diffdata/consent/`, `src/diffdata/scrub/` or `src/diffdata/store/`, this AI review isn't enough: say a human must review before merge.
   - **Solo phase.** The solo phase applies when all three hold: "While Paul works solo" in `CONTRIBUTING.md` says it's in effect, the PR author is Paul (GitHub login `PaulsForge`), and the PR ticks the "Solo phase" box. Then Paul's own read is the human review. Don't flag Paul reviewing their own PR. Use the verdict "Ready once Paul has read every changed line (solo phase)".
   - If the box is ticked but the solo phase has ended, or the author isn't Paul, that's a must fix.
   - If the diff touches those folders, the solo phase is in effect and the author is Paul, but the box isn't ticked, ask for it under must fix: the PR has to say what human review it got.
7. **Description.** Could a teammate who doesn't code follow the What, Why and How it was tested?
8. **CI.** Is it passing?

**Must fix:** breaks a hard rule; real data or a secret; changed behavior without tests; missing or wrong Issue or requirement link; out of lane without the Issue saying so; CI failing; doesn't do what the Issue asks. Everything else is **nice to have**. Don't pad either list, and say "None" when there's nothing.

## 3. Report

Write the review in exactly this shape, short and in plain words:

```
## AI review (separate session)

**Verdict:** Ready to merge | Fix the must-fix items first | Needs a human review (consent, scrub or store) | Ready once Paul has read every changed line (solo phase)

### Must fix
- `path/to/file.py:42`: what's wrong, and what to do about it.

### Nice to have
- None

<sub>Reviewed at <short commit SHA> with /review-pr. Re-run after pushing fixes.</sub>
```

## 4. Post

Show the review, then ask: "Post this as a comment on PR #<n>?" Only on a yes, save it to a temporary file outside the repo and run `gh pr comment <n> --body-file <that file>`.
