# Changelog

Notable changes to `diffdata`. Versions are 0.MINOR.PATCH, matching the phases in [docs/requirements.md](docs/requirements.md) (ENG-4).

## [0.0.1] - 2026-10-08

Repo skeleton, team workflow and requirements (#1).

### Added

- Requirements v0.1 in `docs/requirements.md`.
- `CLAUDE.md` with the hard rules and the rules for Claude, `CONTRIBUTING.md` with the workflow for people new to Git, and the `/review-pr` review routine.
- Pull request template and a Task Issue template.
- The `diffdata` Python package (Python 3.12, uv, ruff, pytest) with one package per stage: consent, discover, collect, organize, scrub and store, plus common.
- The `diffdata` command, with stub subcommands `discover`, `collect`, `scrub` and `push`.
- A data folder guard that refuses to keep collected data inside a git repository, and tests that fail if `.env`, `config.toml` or data folders are ever committed.
- `config.example.toml` and `.env.example`.
- Synthetic test fixtures and a smoke test.
- CI: ruff and pytest on Windows, macOS and Linux for every pull request.
