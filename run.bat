@echo off
cd /d "%~dp0"
if not exist .venv (
  echo First run: setting up Python environment...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -q -r requirements.txt || goto :err
  .venv\Scripts\python -m playwright install chromium || goto :err
)
start "" http://127.0.0.1:8765
.venv\Scripts\python app.py
goto :eof
:err
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
