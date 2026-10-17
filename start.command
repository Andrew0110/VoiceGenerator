#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print "Спочатку відкрийте setup.command."
  read "?Натисніть Enter, щоб закрити вікно."
  exit 1
fi
exec .venv/bin/python scripts/start.py
