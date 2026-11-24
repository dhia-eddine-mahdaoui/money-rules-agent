#!/usr/bin/env sh
# Single command: creates .venv on first run, then runs the test suite.
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m pytest -q "$@"
