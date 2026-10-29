#!/bin/bash
# Запуск панели на macOS двойным щелчком (один раз: chmod +x run-mac.command)
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python3" ]; then PY=".venv/bin/python3"; else PY="python3"; fi
if ! "$PY" -c "import tkinter, PIL" 2>/dev/null; then
    echo "Нужен Python 3 с tkinter и Pillow: $PY -m pip install -r requirements-mac.txt"
    exit 1
fi
nohup "$PY" panel.pyw >/dev/null 2>&1 &
echo "Панель запущена: подведи курсор к верхнему краю экрана или нажми Cmd+Option+P."
