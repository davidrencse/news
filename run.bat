@echo off
cd /d "%~dp0"
set "MEDIUM_LIBRARY_DATA=E:\storage\newsletter-active"
set "TGFS_PROJECT=%USERPROFILE%\Desktop\telegram-file-storage-system"
if not exist .venv (
  echo First run: setting up Python environment...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -q -r requirements.txt || goto :err
  .venv\Scripts\python -m playwright install chromium || goto :err
)
if not exist "%MEDIUM_LIBRARY_DATA%\Medium-Library\library.json" goto :restore
if not exist "%MEDIUM_LIBRARY_DATA%\CVEs\cvelistV5.sqlite3" goto :restore
if not exist "%MEDIUM_LIBRARY_DATA%\search-index\medium.db" goto :restore
goto :start
:restore
echo Restoring the local fast cache from Telegram...
"%TGFS_PROJECT%\.venv\Scripts\python.exe" restore_telegram.py || goto :err
:start
start "" http://127.0.0.1:8765
.venv\Scripts\python app.py
set "APP_EXIT=%ERRORLEVEL%"
echo.
echo Syncing changed newsletter data to Telegram...
"%TGFS_PROJECT%\.venv\Scripts\python.exe" sync_telegram.py
if errorlevel 1 echo Telegram sync failed. Your E: drive copy is still intact.
exit /b %APP_EXIT%
:err
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
