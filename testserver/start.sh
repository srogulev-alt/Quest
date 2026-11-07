#!/usr/bin/env bash
# Запуск сервера теста (Linux/macOS)
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
. venv/bin/activate
if [ -d wheels ]; then pip install --no-index --find-links wheels -r requirements.txt
else pip install -r requirements.txt; fi
exec python app.py
