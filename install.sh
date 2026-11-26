#!/usr/bin/env bash
# diffdata installer for macOS and Linux (#150, item 8). Run in your own Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.sh | bash
#
# or, to hand setup your team file straight away:
#
#   curl -fsSL https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.sh | bash -s -- ~/Downloads/DD-7KQ4.diffdata-team.json
#
# It checks the folder first, then for Git (and starts its install on macOS), installs uv from
# Astral if it's missing, clones this repo to ~/differential-data-collection (or $DIFFDATA_REPO)
# or fast-forwards a clean clone, runs `uv sync`, then hands over to `diffdata setup`.
#
# What it refuses (#150's must-refuse list, approved on 2026-11-26):
#   11  to run as root or under sudo
#   12  to change a folder that isn't a clean clone of this repo on main (no changes, no
#       untracked files, its own work tree), or one inside ~/.diffdata: it stops with nothing
#       changed, before installing anything; an update only fast-forwards
#   13  to read, print, copy or move the team file, or touch ~/.diffdata: it passes the path
#       to setup only
#   14  to go on after a failed step, or to hand over to setup unless its input and output are
#       a terminal, or under Claude Code (CLAUDECODE set): it prints the setup line instead
#   15  to use sudo or edit shell profiles itself, or to fetch the repo or uv from anywhere but
#       GitHub (this repo, with your own Git settings' URL rewrites ignored) and Astral (uv's
#       installer, with its download overrides cleared). uv's installer adds uv to your PATH,
#       as it always does, so `uv run` works in new windows. `uv sync` then installs the
#       locked Python packages from PyPI.

set -euo pipefail

REPO_URL="https://github.com/SPARDiffData/differential-data-collection.git"
DEST="${DIFFDATA_REPO:-$HOME/differential-data-collection}"
while [ "${#DEST}" -gt 1 ] && [ "${DEST%/}" != "$DEST" ]; do DEST="${DEST%/}"; done
TEAM_FILE="${1:-}"
# Settings from the environment that would point Git or uv somewhere else.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR UV_PROJECT_ENVIRONMENT

say() { printf '%s\n' "$*"; }
stop() {
  printf 'diffdata install: %s\n' "$*" >&2
  exit 1
}
# Git for fetching: your own and the system's settings (URL rewrites, hooks) left out.
fetch_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git "$@" </dev/null; }
quoted() { printf '%q' "$1"; }

# 11: never as root.
if [ "$(id -u)" -eq 0 ]; then
  stop "Don't run this as root or with sudo: run the install line as yourself. Nothing was changed."
fi

# The team file's path, made absolute before any change of folder. It's never opened here.
if [ -n "$TEAM_FILE" ] && [ "${TEAM_FILE#/}" = "$TEAM_FILE" ]; then
  TEAM_FILE="$PWD/$TEAM_FILE"
fi

# 12, first part: the folder, before anything is installed.
case "$DEST" in
  /*) ;;
  *) DEST="$PWD/$DEST" ;;
esac
parent="$(cd "${DEST%/*}/" 2>/dev/null && pwd -P || true)"
diffdata_home="$(cd "$HOME/.diffdata" 2>/dev/null && pwd -P || true)"
if [ -n "$diffdata_home" ] && [ -n "$parent" ]; then
  case "$parent/" in
    "$diffdata_home"/*) stop "$DEST is inside ~/.diffdata, which holds your settings and key. Choose another folder. Nothing was changed." ;;
  esac
fi
update=no
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  if [ -L "$DEST" ] || [ ! -d "$DEST" ] || [ ! -e "$DEST/.git" ]; then
    stop "$DEST is there, but isn't a clone of diffdata. Move it away, or set DIFFDATA_REPO to another folder. Nothing was changed."
  fi
  update=yes
fi

# Git. macOS ships a git command that only offers Apple's installer, so check it runs.
if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
  if [ "$(uname -s)" = "Darwin" ]; then
    xcode-select --install >/dev/null 2>&1 || true
    stop "Git is missing. macOS is offering to install Apple's command line tools, which include Git: finish that, then run the install line again. Nothing else was changed."
  fi
  stop "Git is missing. Install it with your system's package manager (see https://git-scm.com/download/linux), then run the install line again. Nothing was changed."
fi

# 12, second part: only a clean clone of this repo, on main, in its own work tree.
if [ "$update" = yes ]; then
  g() { git --no-optional-locks -C "$DEST" "$@" </dev/null; }
  top="$(g rev-parse --show-toplevel 2>/dev/null)" || stop "$DEST isn't a working clone. Nothing was changed."
  if [ "$(cd "$top" && pwd -P)" != "$(cd "$DEST" && pwd -P)" ]; then
    stop "$DEST's Git settings point to another folder. Move it away, or set DIFFDATA_REPO to another folder. Nothing was changed."
  fi
  remote="$(g remote get-url origin 2>/dev/null)" || remote=""
  case "$remote" in
    "https://github.com/SPARDiffData/differential-data-collection" | "$REPO_URL") ;;
    *) stop "$DEST is a clone of another repository. Move it away, or set DIFFDATA_REPO to another folder. Nothing was changed." ;;
  esac
  branch="$(g rev-parse --abbrev-ref HEAD 2>/dev/null)" || branch=""
  if [ "$branch" != "main" ]; then
    stop "$DEST is on branch ${branch:-unknown}, not main. Switch it to main yourself, then run the install line again. Nothing was changed."
  fi
  changes="$(g status --porcelain)" || stop "Couldn't check $DEST for changes. Nothing was changed."
  if [ -n "$changes" ]; then
    stop "$DEST has local changes or files of yours. Keep or move them yourself, then run the install line again. Nothing was changed."
  fi
fi

# uv, called by its full path, so a new PATH in this same window isn't needed.
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}
UV="$(find_uv)"
if [ -z "$UV" ]; then
  say "Installing uv, from Astral (https://astral.sh/uv)..."
  curl -LsSf https://astral.sh/uv/install.sh |
    (unset UV_DOWNLOAD_URL UV_INSTALLER_GITHUB_BASE_URL UV_INSTALLER_GHE_BASE_URL && sh)
  UV="$(find_uv)"
  [ -n "$UV" ] || stop "uv was installed, but this window can't find it. Open a new Terminal and run the install line again."
fi

if [ "$update" = yes ]; then
  say "Updating $DEST..."
  fetch_git -C "$DEST" fetch origin main
  git -C "$DEST" merge --ff-only FETCH_HEAD </dev/null
else
  say "Getting diffdata into $DEST..."
  fetch_git clone --branch main "$REPO_URL" "$DEST"
fi

cd "$DEST"
"$UV" sync </dev/null

if ! command -v claude >/dev/null 2>&1 && [ ! -d "/Applications/Claude.app" ]; then
  say "Claude Code wasn't found. /slack-export needs it: see https://claude.com/claude-code"
fi

# 14: hand over to setup only in a terminal a person types in, never under Claude Code.
setup_line="cd $(quoted "$DEST") && $(quoted "$UV") run diffdata setup"
if [ -n "$TEAM_FILE" ]; then
  setup_line="$setup_line $(quoted "$TEAM_FILE")"
fi
if [ -n "${CLAUDECODE+x}" ] || [ ! -t 1 ] || ! { exec 3</dev/tty; } 2>/dev/null; then
  say "Installed. Now run setup yourself, in your own Terminal:"
  say "  $setup_line"
  exit 0
fi
exec 3<&-
say "Installed. Starting setup..."
if [ -n "$TEAM_FILE" ]; then
  exec "$UV" run diffdata setup "$TEAM_FILE" </dev/tty
fi
exec "$UV" run diffdata setup </dev/tty
