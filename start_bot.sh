#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
pip install -r requirements.txt >/dev/null

pkill -f 'python.*bot.py' || true
exec .venv/bin/python bot.py
