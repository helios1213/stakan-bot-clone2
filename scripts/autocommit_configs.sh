#!/usr/bin/env bash
# Auto-commit live per-pair config changes to git so the repo always mirrors the
# running primary bot. CONFIG-ONLY by design: it never stages src/ or anything
# else, so it can never accidentally push code. Safe to run on a short cron.
set -uo pipefail
REPO=/root/stakan-bot
LOG=/root/stakan-bot/logs/autocommit.log
cd "$REPO" || exit 0

# Only these config paths (tuning lives here). NOT src/, NOT data/, NOT secrets.
PATHS=(config/pairs config/global.yaml config/config.yaml)

# Nothing changed among tracked config files? exit quietly.
if git diff --quiet -- "${PATHS[@]}" 2>/dev/null; then
  exit 0
fi

git add -- "${PATHS[@]}" 2>/dev/null
# If the add produced nothing staged (e.g. only ignored files), bail.
if git diff --cached --quiet; then
  exit 0
fi

TS=$(date -u +"%Y-%m-%d %H:%M UTC")
git commit -q -m "config(auto): live config snapshot ${TS}" 2>>"$LOG" || exit 0
if git push -q origin main 2>>"$LOG"; then
  echo "$(date -u +%FT%TZ) pushed config snapshot" >> "$LOG"
else
  echo "$(date -u +%FT%TZ) PUSH FAILED (committed locally; will retry next run)" >> "$LOG"
fi
