#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/marcos/.openclaw/workspace/yt-automator/backend"
VENV="$ROOT/.venv/bin/activate"
PIDFILE="$ROOT/.yt-automator.pid"
LOGFILE="/tmp/yt-automator-8020.log"
PORT="8020"

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
  # shellcheck source=/dev/null
  source "$VENV"
  nohup uvicorn main:app --host 0.0.0.0 --port "$PORT" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if is_running; then
    echo "started pid=$(cat "$PIDFILE") port=$PORT"
  else
    echo "failed to start"
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
    exit 0
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
    exit 1
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
