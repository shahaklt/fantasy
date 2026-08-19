#!/usr/bin/env bash
# Update Gridiron in place.
#
# Pulls the latest code and syncs dependencies *only if they changed*. Your
# downloaded NFL data, saved league settings and credentials all live in
# gitignored directories, so none of it is touched or re-downloaded.
set -euo pipefail
cd "$(dirname "$0")"

VENV=${VENV:-.venv}
BRANCH=${BRANCH:-}

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m==>\033[0m %s\n' "$1"; }

# --- refuse to clobber local edits -------------------------------------------
if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "You have uncommitted changes. Stash or commit them first:"
  git status --short | sed 's/^/    /'
  exit 1
fi

CURRENT=$(git rev-parse --abbrev-ref HEAD)
BRANCH=${BRANCH:-$CURRENT}
BEFORE=$(git rev-parse HEAD)
# Hash the dependency spec so we can skip pip when nothing changed.
DEPS_BEFORE=$(git hash-object pyproject.toml)

say "Fetching origin/$BRANCH"
for attempt in 1 2 3 4; do
  if git fetch origin "$BRANCH"; then break; fi
  warn "fetch failed, retrying in $((2 ** attempt))s"
  sleep $((2 ** attempt))
done

git merge --ff-only "origin/$BRANCH" 2>/dev/null || {
  warn "Cannot fast-forward — your branch has diverged from origin/$BRANCH."
  warn "Resolve with: git pull --rebase origin $BRANCH"
  exit 1
}

AFTER=$(git rev-parse HEAD)
if [ "$BEFORE" = "$AFTER" ]; then
  say "Already up to date ($(git log -1 --format=%h\ %s))"
else
  say "Updated $(git rev-parse --short "$BEFORE") -> $(git rev-parse --short "$AFTER")"
  git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

# --- dependencies -------------------------------------------------------------
# The package is installed editable, so code changes are already live. Only a
# changed dependency spec needs pip to run at all.
if [ ! -d "$VENV" ]; then
  warn "No virtualenv at $VENV — run ./run.sh instead for a first-time setup."
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ "$DEPS_BEFORE" != "$(git hash-object pyproject.toml)" ]; then
  say "Dependencies changed — syncing (cached wheels, no data re-download)"
  pip install --upgrade-strategy only-if-needed -e ".[dev]"
else
  say "Dependencies unchanged — skipping pip"
fi

# --- report what survived ------------------------------------------------------
CACHE_SIZE=$(du -sh data/cache 2>/dev/null | cut -f1 || echo "0")
say "Kept $CACHE_SIZE of downloaded data, plus league settings and credentials"

if [ "$BEFORE" != "$AFTER" ]; then
  echo
  say "Rebuild simulations to pick up model changes:"
  echo "    gridiron build --sims 20000"
  echo "  (or just press 'Rebuild sims' in the web app)"
fi
