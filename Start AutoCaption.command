#!/bin/bash
# Mac launcher — double-click to start AutoCaption.
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is missing. Install it once with:  brew install ffmpeg"
  echo "(No Homebrew? Get it at https://brew.sh first.)"; read -n1 -r -p "Press any key to close"; exit 1
fi
if [ ! -d .venv ]; then
  echo "First run: setting things up (takes a minute)…"
  python3 -m venv .venv || { echo "Python 3 is missing. Install it from https://python.org"; read -n1; exit 1; }
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/python app.py
