#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}"

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "Error: uvicorn is not installed or not on PATH."
  echo "Install deps first: pip install -r server/requirements.txt"
  exit 1
fi

uvicorn server.app:app --reload --host "$HOST" --port "$PORT" &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in {1..50}; do
  if curl -fsS "$URL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
elif command -v start >/dev/null 2>&1; then
  start "$URL"
else
  echo "Could not find a browser opener command (open/xdg-open/start)."
  echo "Open this URL manually: $URL"
fi

wait "$SERVER_PID"
