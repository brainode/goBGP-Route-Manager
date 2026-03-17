#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python interpreter not found: $PYTHON_BIN" >&2
  exit 1
fi

echo "Creating virtual environment in: $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "Upgrading pip"
"$VENV_DIR/bin/python" -m pip install --upgrade pip

echo "Installing dependencies from requirements.txt"
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"

echo
echo "Virtual environment is ready."
echo "Activate it with: source venv/bin/activate"
