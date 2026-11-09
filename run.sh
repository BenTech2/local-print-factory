#!/usr/bin/env bash
# Run Local Print Factory without Docker:  ./run.sh   then open http://<this-machine>:8800
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8800}"
