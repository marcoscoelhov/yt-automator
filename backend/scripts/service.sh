#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd -- "${ROOT}/.." && pwd)"
PYTHON_BIN="$ROOT/.venv/bin/python"
PIDFILE="$PROJECT_ROOT/.yt-automator.pid"
PORT="${YT_AUTOMATOR_PORT:-8020}"
LOGFILE="/tmp/yt-automator-${PORT}.log"

wait_for_health() {
  local attempts=0
  while [[ $attempts -lt 20 ]]; do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    attempts=$((attempts + 1))
  done
  return 1
}

detect_port_pid() {
  ss -ltnp 2>/dev/null | awk -v p=":${PORT} " '$0 ~ p {print $NF}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -n1
}

is_running() {
  local pid=""
  if [[ -f "$PIDFILE" ]]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi

  pid=$(detect_port_pid || true)
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
  : > "$LOGFILE"
  setsid "$PYTHON_BIN" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" </dev/null >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  disown || true
  if wait_for_health && is_running; then
    echo "started pid=$(cat "$PIDFILE") port=$PORT"
  else
    echo "failed to start"
    tail -n 80 "$LOGFILE" || true
    exit 1
  fi
}

stop() {
  local pid=""
  if is_running; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
  else
    pid=$(detect_port_pid || true)
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

health() {
  curl -fsS "http://127.0.0.1:${PORT}/health"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop || true; start ;;
  status) status ;;
  logs) logs ;;
  health) health ;;
  *) echo "use: $0 {start|stop|restart|status|logs|health}"; exit 2 ;;
esac
