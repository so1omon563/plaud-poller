#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
cd "${REPO_ROOT}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
# A failed browser re-exchange emits one auth error, then scheduled cooldown
# runs stay quiet until the next bounded automatic retry window.
export PLAUD_SILENCE_AUTH_COOLDOWN=true
exec "${PYTHON_BIN}" -m plaud_poller.poll "$@"
