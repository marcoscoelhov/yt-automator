#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB="20 3 * * * ${SCRIPT_DIR}/housekeep.sh >> /tmp/yt-automator-housekeep.log 2>&1"
( { crontab -l 2>/dev/null || true; } | grep -v 'yt-automator/backend/scripts/housekeep.sh' || true; echo "$JOB" ) | crontab -
echo "housekeep cron instalado: $JOB"
