#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  python3 -m venv venv
  . venv/bin/activate
  pip install -r requirements.txt
else
  . venv/bin/activate
fi
[ -f .env ] || cp .env.example .env
ulimit -n 16384 2>/dev/null || ulimit -n 8192 2>/dev/null
python main.py
