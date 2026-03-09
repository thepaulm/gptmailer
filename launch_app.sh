#!/usr/bin/env bash
set -euo pipefail

# Load server/.env so PORT/HOST and other runtime vars can be configured there.
if [[ -f "server/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "server/.env"
  set +a
fi

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
SSL_CERTFILE="${SSL_CERTFILE:-}"
SSL_KEYFILE="${SSL_KEYFILE:-}"
SCHEME="http"
CHECK_HOST="$HOST"
if [[ "$CHECK_HOST" == "0.0.0.0" ]]; then
  CHECK_HOST="127.0.0.1"
fi
OPEN_BROWSER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --open)
      OPEN_BROWSER=true
      shift
      ;;
    -h|--help)
      echo "Usage: ./launch_app.sh [--open]"
      echo "  --open    Open the app URL in your default browser"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: ./launch_app.sh [--open]"
      exit 1
      ;;
  esac
done

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "Error: uvicorn is not installed or not on PATH."
  echo "Install deps first: pip install -r server/requirements.txt"
  exit 1
fi

if [[ -n "$SSL_CERTFILE" || -n "$SSL_KEYFILE" ]]; then
  if [[ -z "$SSL_CERTFILE" || -z "$SSL_KEYFILE" ]]; then
    echo "Error: both SSL_CERTFILE and SSL_KEYFILE must be set together."
    exit 1
  fi
  SCHEME="https"
fi

URL="${SCHEME}://${HOST}:${PORT}"
HEALTH_URL="${SCHEME}://${CHECK_HOST}:${PORT}"

UVICORN_ARGS=(server.app:app --reload --host "$HOST" --port "$PORT")
if [[ "$SCHEME" == "https" ]]; then
  UVICORN_ARGS+=(--ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE")
fi

uvicorn "${UVICORN_ARGS[@]}" &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in {1..50}; do
  if [[ "$SCHEME" == "https" ]]; then
    if curl -kfsS "$HEALTH_URL" >/dev/null 2>&1; then
      break
    fi
  else
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      break
    fi
  fi
  sleep 0.2
done

if [[ "$OPEN_BROWSER" == "true" ]] && command -v open >/dev/null 2>&1; then
  open "$URL"
elif [[ "$OPEN_BROWSER" == "true" ]] && command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
elif [[ "$OPEN_BROWSER" == "true" ]] && command -v start >/dev/null 2>&1; then
  start "$URL"
elif [[ "$OPEN_BROWSER" == "true" ]]; then
  echo "Could not find a browser opener command (open/xdg-open/start)."
  echo "Open this URL manually: $URL"
else
  echo "Server is up at: $URL"
  echo "Tip: pass --open to launch the browser automatically."
fi

wait "$SERVER_PID"
