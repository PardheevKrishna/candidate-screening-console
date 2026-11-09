#!/usr/bin/env bash
#
# Creates the Python virtual environment for the Candidate Screening & Integrity Console and
# installs its pinned dependencies (Linux; also works in Git Bash on Windows).
#
# Usage:
#   bash scripts/setup.sh [options]
#
# Options:
#   --venv PATH          Virtual environment folder (default: ~/.venvs/hiring-screening).
#                        Keep it outside synced folders, network shares and the project folder,
#                        and create it on the machine that runs the app.
#   --index-url URL      Package index, e.g. an internal mirror (https://pypi.internal.example/simple).
#   --trusted-host HOST  Trust this index host without TLS verification (internal mirror only).
#   --wheel-dir DIR      Install offline from a folder of wheels (pip --no-index --find-links).
#   --python EXE         Python 3.11 to 3.14 (64-bit) interpreter used to create the environment.
#   --no-tests           Skip the test suite after installing.
#   --dry-run            Print the steps without changing anything.
#   -h, --help           Show this help.
#
# pip also honours HTTPS_PROXY, PIP_INDEX_URL, PIP_TRUSTED_HOST and PIP_CERT.
# Full installation guide: docs/DEPLOYMENT.md
set -euo pipefail

MIN_MINOR=11   # requirements.txt is tested on Python 3.11 to 3.14
MAX_MINOR=14
SUPPORTED="3.${MIN_MINOR} to 3.${MAX_MINOR}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.venvs/hiring-screening"
INDEX_URL=""
TRUSTED_HOST=""
WHEEL_DIR=""
PYTHON=""
RUN_TESTS=1
DRY_RUN=0

usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
step() { printf '==> %s\n' "$*"; }
hide_credentials() { sed -E 's#(://)[^/@[:space:]]+@#\1***@#g'; }
need_value() { [[ -n "${2:-}" ]] || die "Option $1 needs a value (see --help)."; }

# Runs a command, or only prints it with --dry-run.
run() {
  if (( DRY_RUN )); then
    { printf '[dry-run]'; printf ' %q' "$@"; printf '\n'; } | hide_credentials
    return 0
  fi
  "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv) need_value "$@"; VENV="$2"; shift 2 ;;
    --venv=*) VENV="${1#*=}"; shift ;;
    --index-url) need_value "$@"; INDEX_URL="$2"; shift 2 ;;
    --index-url=*) INDEX_URL="${1#*=}"; shift ;;
    --trusted-host) need_value "$@"; TRUSTED_HOST="$2"; shift 2 ;;
    --trusted-host=*) TRUSTED_HOST="${1#*=}"; shift ;;
    --wheel-dir) need_value "$@"; WHEEL_DIR="$2"; shift 2 ;;
    --wheel-dir=*) WHEEL_DIR="${1#*=}"; shift ;;
    --python) need_value "$@"; PYTHON="$2"; shift 2 ;;
    --python=*) PYTHON="${1#*=}"; shift ;;
    --no-tests) RUN_TESTS=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

# Prints "MAJOR.MINOR.MICRO BITS" for an interpreter, or nothing when it cannot be run.
py_info() {
  "$1" -c 'import sys, struct; print("%d.%d.%d %d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2], struct.calcsize("P") * 8))' 2>/dev/null || true
}

# Interpreter inside a virtual environment (Linux layout, or Windows layout under Git Bash).
venv_python() {
  local candidate
  for candidate in "$1/bin/python" "$1/Scripts/python.exe"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

find_python() {
  local candidates=() candidate info version bits minor seen=""
  if [[ -n "$PYTHON" ]]; then
    candidates=("$PYTHON")
  else
    local m
    for (( m = MIN_MINOR; m <= MAX_MINOR; m++ )); do candidates+=("python3.${m}"); done
    candidates+=(python3 python)
  fi
  for candidate in "${candidates[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    info="$(py_info "$candidate")"
    [[ -n "$info" ]] || continue
    version="${info% *}"
    bits="${info#* }"
    seen+=" ${candidate} = Python ${version} (${bits}-bit);"
    [[ "$bits" == "64" && "${version%%.*}" == "3" ]] || continue
    minor="$(printf '%s' "$version" | cut -d. -f2)"
    if (( minor >= MIN_MINOR && minor <= MAX_MINOR )); then
      printf '%s\n' "$candidate"
      return 0
    fi
    if [[ -n "$PYTHON" ]] && (( minor > MAX_MINOR )); then
      warn "Python $version is newer than the tested versions (${SUPPORTED}); continuing because --python was given."
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  die "Python ${SUPPORTED} (64-bit) is required.${seen:+ Found:${seen}} Install one of them (plus the matching python3.X-venv package on Debian/Ubuntu) or pass --python PATH."
}

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
[[ -f "$ROOT/requirements.txt" ]] || die "requirements.txt not found in $ROOT."
[[ -z "$WHEEL_DIR" || -z "$INDEX_URL" ]] || die "Use either --wheel-dir (offline install) or --index-url (package mirror), not both."
[[ -z "$WHEEL_DIR" || -d "$WHEEL_DIR" ]] || die "Wheel folder not found: $WHEEL_DIR"

case "$VENV" in
  /*|[A-Za-z]:*) ;;
  *) VENV="$PWD/$VENV" ;;
esac
VENV="${VENV%/}"
case "/$VENV/" in
  */OneDrive/*|*/"OneDrive - "*|*/Dropbox/*|*/"Google Drive"/*)
    warn "$VENV looks like a cloud-synced folder. Syncing locks and corrupts virtual environments; use a local folder such as /opt/venvs/hiring-screening." ;;
esac
case "$VENV/" in
  "$ROOT/"*) warn "The virtual environment is inside the project folder. Keep it outside, so it is never copied or replaced together with the code." ;;
esac

# ---------------------------------------------------------------------------
# 1-2. Python and the virtual environment
# ---------------------------------------------------------------------------
if VPY="$(venv_python "$VENV")"; then
  step "Using the existing virtual environment at $VENV"
  info="$(py_info "$VPY")"
  [[ -n "$info" ]] || die "The virtual environment at $VENV does not start (was its base Python removed or moved?). Delete that folder and run this script again."
  vminor="$(printf '%s' "${info% *}" | cut -d. -f2)"
  if [[ "${info%%.*}" != "3" ]] || (( vminor < MIN_MINOR || vminor > MAX_MINOR )); then
    warn "It uses Python ${info% *}; requirements.txt is tested on Python ${SUPPORTED}."
  fi
else
  BASE_PY="$(find_python)"
  step "Creating the virtual environment at $VENV with $BASE_PY (Python $(py_info "$BASE_PY" | cut -d' ' -f1))"
  run mkdir -p "$(dirname "$VENV")"
  if ! run "$BASE_PY" -m venv "$VENV"; then
    die "Creating the virtual environment failed. On Debian/Ubuntu install the matching python3.X-venv package first."
  fi
  if (( DRY_RUN )); then
    case "$(uname -s 2>/dev/null || echo unknown)" in
      MINGW*|MSYS*|CYGWIN*) VPY="$VENV/Scripts/python.exe" ;;
      *) VPY="$VENV/bin/python" ;;
    esac
  else
    VPY="$(venv_python "$VENV")" || die "No interpreter found in $VENV after creating the virtual environment."
  fi
fi

if [[ -x "$VPY" ]] && ! "$VPY" -m pip --version >/dev/null 2>&1; then
  step "pip is missing in the virtual environment; adding it with ensurepip (works offline)"
  run "$VPY" -m ensurepip --upgrade --default-pip || die "Could not install pip into $VENV."
fi

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
PIP_ARGS=(-m pip install --disable-pip-version-check --requirement "$ROOT/requirements.txt")
if [[ -n "$WHEEL_DIR" ]]; then
  PIP_ARGS+=(--no-index --find-links "$WHEEL_DIR")
elif [[ -n "$INDEX_URL" ]]; then
  PIP_ARGS+=(--index-url "$INDEX_URL")
fi
if [[ -n "$TRUSTED_HOST" ]]; then
  PIP_ARGS+=(--trusted-host "$TRUSTED_HOST")
fi

step "Installing dependencies from requirements.txt"
run "$VPY" "${PIP_ARGS[@]}" || die "pip install failed. See 'Troubleshooting' in docs/DEPLOYMENT.md (proxy, certificates, package mirror, offline wheels)."
run "$VPY" -m pip check || die "pip check reported inconsistent dependencies (see above)."

# ---------------------------------------------------------------------------
# 4. Tests
# ---------------------------------------------------------------------------
if (( RUN_TESTS )); then
  step "Running the test suite (about a minute)"
  ( cd "$ROOT" && run "$VPY" -m pytest -p no:cacheprovider ) || die "The test suite failed. Fix the problems above before starting the app."
else
  step "Skipping the test suite (--no-tests)."
fi

if (( DRY_RUN )); then
  step "Dry run (--dry-run): nothing was changed."
  exit 0
fi
step "Done."
echo
echo "Start the app with:"
printf '  bash %q --venv %q\n' "$ROOT/scripts/run_app.sh" "$VENV"
echo "Add --address 0.0.0.0 to accept connections from other machines (read \"Network and security\" in docs/DEPLOYMENT.md first)."
