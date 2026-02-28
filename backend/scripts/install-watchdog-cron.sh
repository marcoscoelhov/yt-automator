#!/usr/bin/env bash
set -euo pipefail

JOB="*/2 * * * * /home/marcos/.openclaw/workspace/yt-automator/backend/scripts/watchdog.sh"
( crontab -l 2>/dev/null | grep -v 'yt-automator/backend/scripts/watchdog.sh' ; echo "$JOB" ) | crontab -
echo "watchdog cron instalado: $JOB"
