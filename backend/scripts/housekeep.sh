#!/usr/bin/env bash
set -euo pipefail

PORT="${YT_AUTOMATOR_PORT:-8020}"

curl -fsS -X POST "http://127.0.0.1:${PORT}/ops/cleanup"
