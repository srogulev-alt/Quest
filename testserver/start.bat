@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv (
  python -m venv venv || (echo Не найден Python 3. Установите его с python.org и повторите. & pause & exit /b 1)
)
call venv\Scripts\activate
if exist wheels (
  pip install --no-index --find-links wheels -r requirements.txt
) else (
  pip install -r requirements.txt
)
python app.py
pause
