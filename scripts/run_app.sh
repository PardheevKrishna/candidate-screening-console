#!/usr/bin/env bash
#
# Starts the Candidate Screening & Integrity Console (Streamlit) in the foreground from its
# virtual environment (Linux; also works in Git Bash on Windows). Stop it with Ctrl+C or
# through the service manager.
#
# Usage:
#   bash scripts/run_app.sh [--venv PATH] [--port N] [--address IP] [--public-host NAME]
#
# Options:
#   --venv PATH          Virtual environment folder (default: ~/.venvs/hiring-screening).
#   --port N             TCP port (default: 8501).
#   --address IP         Interface to listen on. 127.0.0.1 (default) accepts connections from
#                        this machine only, e.g. a reverse proxy on the same VM. 0.0.0.0 accepts
#                        other machines: restrict the port with the firewall (the app has no login).
#   --public-host NAME   Host name users type in the browser (sets browser.serverAddress, used for
#                        the printed URL and the origin checks; also skips Streamlit's public-IP
#                        lookup at startup).
#   -h, --help           Show this help.
#
# Always passed: --server.headless true, --server.fileWatcherType none and
# --browser.gatherUsageStats false. Everything else comes from .streamlit/config.toml or
# STREAMLIT_* environment variables. See docs/DEPLOYMENT.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.venvs/hiring-screening"
PORT=8501
ADDRESS="127.0.0.1"
PUBLIC_HOST=""

usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need_value() { [[ -n "${2:-}" ]] || die "Option $1 needs a value (see --help)."; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv) need_value "$@"; VENV="$2"; shift 2 ;;
    --venv=*) VENV="${1#*=}"; shift ;;
    --port) need_value "$@"; PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --address) need_value "$@"; ADDRESS="$2"; shift 2 ;;
    --address=*) ADDRESS="${1#*=}"; shift ;;
    --public-host) need_value "$@"; PUBLIC_HOST="$2"; shift 2 ;;
    --public-host=*) PUBLIC_HOST="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && (( 10#$PORT >= 1 && 10#$PORT <= 65535 )) || die "--port must be a number between 1 and 65535."
PORT=$(( 10#$PORT ))
[[ -n "$ADDRESS" ]] || die "--address must not be empty."
[[ -f "$ROOT/app.py" ]] || die "app.py not found in $ROOT."

VPY=""
for candidate in "$VENV/bin/python" "$VENV/Scripts/python.exe"; do
  if [[ -x "$candidate" ]]; then
    VPY="$candidate"
    break
  fi
done
[[ -n "$VPY" ]] || die "No virtual environment at $VENV. Run scripts/setup.sh first, or pass --venv."

if [[ "$(id -u 2>/dev/null || echo 1)" == "0" ]]; then
  printf 'WARNING: %s\n' "Running as root is not recommended; use a dedicated service account." >&2
fi

# Fail early when the port is taken (best effort; needs the ss tool from iproute2).
if command -v ss >/dev/null 2>&1 && ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
  die "Port $PORT is already in use (see: ss -ltnp 'sport = :$PORT'). Stop that process or choose another --port."
fi

ARGS=(-m streamlit run app.py
      --server.port "$PORT"
      --server.address "$ADDRESS"
      --server.headless true
      --server.fileWatcherType none
      --browser.gatherUsageStats false)
if [[ -n "$PUBLIC_HOST" ]]; then
  ARGS+=(--browser.serverAddress "$PUBLIC_HOST")
fi

# The app must run from the project folder: .streamlit/ (config and secrets) and the .cache/
# folder are resolved from the working directory.
cd "$ROOT"
export PYTHONUNBUFFERED=1   # log lines reach the journal / log files immediately
echo "Starting the app from $ROOT (listening on $ADDRESS:$PORT). Press Ctrl+C to stop."
exec "$VPY" "${ARGS[@]}"
