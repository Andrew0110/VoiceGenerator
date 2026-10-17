#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  task_python="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || true)"
  if [[ -z "$task_python" ]]; then
    print "Потрібен Python 3.10–3.12. Установіть його з python.org, а потім відкрийте цей файл знову."
    read "?Натисніть Enter, щоб закрити вікно."
    exit 1
  fi
  "$task_python" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python scripts/download_model.py
read "?Налаштування завершене. Натисніть Enter, щоб закрити вікно."
