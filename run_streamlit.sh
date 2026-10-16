#!/bin/sh
# Local clinician workspace; keeps the existing AI agent and .env configuration.
set -eu
APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$APP_DIR"
if [ ! -x .venv/bin/python ]; then
    "${PYTHON_BIN:-python3}" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements-streamlit.txt
exec .venv/bin/python -m streamlit run streamlit_app.py --server.address 127.0.0.1 "$@"
