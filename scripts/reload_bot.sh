#!/usr/bin/env bash
set -euo pipefail

# Location-independent reload script for the bot.
# Usage: bash scripts/reload_bot.sh [HANDBOOK_PATH]

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repo root (bot repo)
REPO_ROOT="$(cd "$DIR/.." && pwd)"

# Optionally pass HANDBOOK_PATH as first arg or via env
HANDBOOK_PATH="${1:-${HANDBOOK_PATH:-$REPO_ROOT/../Handbook_MVP_File_Search}}"

echo "📥 Updating handbook at: $HANDBOOK_PATH"
if [ -d "$HANDBOOK_PATH" ]; then
  pushd "$HANDBOOK_PATH" >/dev/null
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git stash --quiet || true
    git pull origin main || true
  else
    echo "⚠️ $HANDBOOK_PATH is not a git repo"
  fi
  popd >/dev/null
else
  echo "⚠️ Handbook path does not exist: $HANDBOOK_PATH"
fi

echo "🛑 Stopping bot process if running..."
pkill -f "python.*bot.py" || echo "No bot process found"
sleep 2

echo "🚀 Starting bot from $REPO_ROOT"
cd "$REPO_ROOT"
if [ -f ".venv/bin/activate" ]; then
  # Activate venv and run in background
  # shellcheck disable=SC1091
  source .venv/bin/activate
  nohup python bot.py > /tmp/bot.log 2>&1 &
else
  # fallback to system python
  nohup python3 bot.py > /tmp/bot.log 2>&1 &
fi

sleep 2
echo "✅ Bot restarted (logs at /tmp/bot.log)"
