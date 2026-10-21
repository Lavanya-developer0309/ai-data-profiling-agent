#!/bin/bash
# Start the web app:  ./run.sh   ->  http://127.0.0.1:5050
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
# local AI (Ollama) - start it if it isn't running
command -v ollama >/dev/null && ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null && (ollama serve >/dev/null 2>&1 &) && sleep 2
.venv/bin/python app.py
