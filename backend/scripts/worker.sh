#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd -- "${ROOT}/.." && pwd)"
PYTHON_BIN="$ROOT/.venv/bin/python"
PIDFILE="$PROJECT_ROOT/.yt-automator-worker.pid"
LOGFILE="/tmp/yt-automator-worker.log"
ENVFILE="$ROOT/.env"
export PATH="$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

load_env() {
  if [[ -f "$ENVFILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENVFILE"
    set +a
  fi
}

detect_worker_pid() {
  ps -eo pid=,args= | awk -v root="$ROOT" '$0 ~ /worker\.py/ && index($0, root) {print $1; exit}'
}

is_running() {
  local pid=""
  if [[ -f "$PIDFILE" ]]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi

  pid=$(detect_worker_pid || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid" > "$PIDFILE"
    return 0
  fi
  return 1
}

start() {
  if is_running; then
    echo "already running pid=$(cat "$PIDFILE")"
    exit 0
  fi
  cd "$ROOT"
  load_env
  : > "$LOGFILE"
  setsid "$PYTHON_BIN" worker.py </dev/null >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  disown || true
  sleep 2
  if is_running; then
    echo "started pid=$(cat "$PIDFILE")"
  else
    echo "failed to start worker"
    tail -n 80 "$LOGFILE" || true
    exit 1
  fi
}

stop() {
  local pid=""
  if is_running; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
  else
    pid=$(detect_worker_pid || true)
  fi

  if [[ -z "${pid:-}" ]]; then
    echo "not running"
    rm -f "$PIDFILE"
    return 0
  fi

  kill "$pid" || true
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "stopped pid=$pid"
}

status() {
  if is_running; then
    echo "running pid=$(cat "$PIDFILE")"
  else
    echo "stopped"
    return 1
  fi
}

logs() {
  tail -n 120 "$LOGFILE"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop || true; start ;;
  status) status ;;
  logs) logs ;;
  *) echo "use: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
