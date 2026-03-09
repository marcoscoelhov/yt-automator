#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SERVICE="$ROOT/scripts/service.sh"
WORKER="$ROOT/scripts/worker.sh"
PORT="${YT_AUTOMATOR_PORT:-8020}"

if ! curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "[$(date -Iseconds)] health failed, restarting yt-automator" >> /tmp/yt-automator-watchdog.log
  "$SERVICE" restart >> /tmp/yt-automator-watchdog.log 2>&1 || true
fi

if ! "$WORKER" status >/dev/null 2>&1; then
  echo "[$(date -Iseconds)] worker stopped, restarting yt-automator worker" >> /tmp/yt-automator-watchdog.log
  "$WORKER" restart >> /tmp/yt-automator-watchdog.log 2>&1 || true
fi
