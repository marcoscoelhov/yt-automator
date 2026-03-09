#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB="*/2 * * * * ${SCRIPT_DIR}/watchdog.sh"
( { crontab -l 2>/dev/null || true; } | grep -v 'yt-automator/backend/scripts/watchdog.sh' || true; echo "$JOB" ) | crontab -
echo "watchdog cron instalado: $JOB"
