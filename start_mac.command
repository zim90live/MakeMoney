#!/bin/zsh
set -e

cd "$(dirname "$0")"

PORT="${PORT:-5057}"
URL="http://127.0.0.1:${PORT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Please install Python 3 first."
  read -k 1 "?Press any key to exit..."
  exit 1
fi

if ! python3 -c "import flask, yaml" >/dev/null 2>&1; then
  echo "Missing dependencies. Please run:"
  echo "  pip install -r engine/requirements.txt"
  read -k 1 "?Press any key to exit..."
  exit 1
fi

if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    PIDS_CSV=${PIDS//$'\n'/,}
    echo "Port ${PORT} is already in use:"
    ps -p "$PIDS_CSV" -o pid=,command= 2>/dev/null | sed 's/^/  /'
    echo ""

    MINE=1
    for pid in ${=PIDS}; do
      CMD=$(ps -p "$pid" -o command= 2>/dev/null || true)
      case "$CMD" in
        *engine/app.py*) ;;
        *) MINE=0 ;;
      esac
    done

    if [ "$MINE" -eq 1 ]; then
      echo "Stopping old dashboard process: ${PIDS_CSV} ..."
      kill ${=PIDS} 2>/dev/null || true
      for i in {1..10}; do
        lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1 || break
        sleep 0.5
      done
      if lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Process did not exit; forcing shutdown..."
        kill -9 ${=PIDS} 2>/dev/null || true
        sleep 1
      fi
      if lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Could not release port ${PORT}. Try another port:"
        echo "  PORT=5058 ./start_mac.command"
        read -k 1 "?Press any key to exit..."
        exit 1
      fi
      echo "Port ${PORT} released; continuing."
    else
      echo "Port ${PORT} is used by another program, so it was not stopped."
      echo "Try another port:"
      echo "  PORT=5058 ./start_mac.command"
      read -k 1 "?Press any key to exit..."
      exit 1
    fi
  fi
fi

echo "Starting investment dashboard: ${URL}"
(sleep 1.5; open "${URL}") >/dev/null 2>&1 &
python3 engine/app.py
