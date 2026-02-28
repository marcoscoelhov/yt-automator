#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/marcos/.openclaw/workspace/yt-automator/backend"
SERVICE="$ROOT/scripts/service.sh"
PORT=8020

if ! curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "[$(date -Iseconds)] health failed, restarting yt-automator" >> /tmp/yt-automator-watchdog.log
  "$SERVICE" restart >> /tmp/yt-automator-watchdog.log 2>&1 || true
fi
