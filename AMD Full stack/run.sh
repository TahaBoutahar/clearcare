#!/bin/sh
# Start the dashboard on http://localhost:8000
#   ./run.sh            (PIPELINE_DIR defaults to the project folder above this one)
#   PORT=9000 ./run.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="${PIPELINE_DIR:-$(cd "$HERE/.." && pwd)}"
export PIPELINE_DIR

if [ ! -x "$HERE/.venv/bin/python" ]; then
    echo "Creating virtual environment…"
    python3 -m venv "$HERE/.venv"
fi
"$HERE/.venv/bin/pip" install -q --disable-pip-version-check -r "$PIPELINE_DIR/requirements.txt" -r "$HERE/requirements.txt"

cd "$HERE/backend"
exec "$HERE/.venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "${PORT:-8000}"
