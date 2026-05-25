#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-}"
SKIP_INSTALL_CHECK="${SKIP_INSTALL_CHECK:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRY_POINT="$REPO_ROOT/src/beautiful_linkedin/server/__main__.py"
DIST_PATH="$REPO_ROOT/dist"
WORK_PATH="$REPO_ROOT/build/pyinstaller"

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PYTHON_VERSION" in
  3.11|3.12) ;;
  *)
    echo "Warning: Python $PYTHON_VERSION can package this checkout, but Python 3.11/3.12 is recommended for release builds." >&2
    ;;
esac

if [[ "$SKIP_INSTALL_CHECK" != "1" ]]; then
  if ! "$PYTHON_BIN" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "PyInstaller is not installed for '$PYTHON_BIN'." >&2
    echo 'Run: '"$PYTHON_BIN"' -m pip install -e ".[server,dev,risky,scrapling]"' >&2
    exit 1
  fi
fi

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --name beautiful-linkedin-sidecar \
  --onedir \
  --console \
  --paths "$REPO_ROOT/src" \
  --distpath "$DIST_PATH" \
  --workpath "$WORK_PATH" \
  --collect-all beautiful_linkedin \
  --collect-all fastapi \
  --collect-all uvicorn \
  --collect-all pydantic \
  --collect-all pandas \
  "$ENTRY_POINT"

SIDECAR_BIN="$DIST_PATH/beautiful-linkedin-sidecar/beautiful-linkedin-sidecar"
if [[ ! -x "$SIDECAR_BIN" ]]; then
  echo "Expected sidecar executable was not created: $SIDECAR_BIN" >&2
  exit 1
fi

echo "Built sidecar: $SIDECAR_BIN"
