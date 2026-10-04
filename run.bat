@echo off
cd /d "%~dp0"
if not defined MEDIUM_LIBRARY_DATA (
  if exist "E:\" (
    set "MEDIUM_LIBRARY_DATA=E:\storage\newsletter-active"
  ) else (
    set "MEDIUM_LIBRARY_DATA=%LOCALAPPDATA%\LibraryOfBabel"
  )
)
if not defined TGFS_PROJECT set "TGFS_PROJECT=%USERPROFILE%\Desktop\telegram-file-storage-system"
if not exist "%TGFS_PROJECT%\src\tgfs" goto :tgfs_missing
if not exist "%TGFS_PROJECT%\.venv\Scripts\python.exe" goto :tgfs_python_missing
if not exist .venv (
  echo First run: setting up Python environment...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -q -r requirements.txt || goto :err
  .venv\Scripts\python -m playwright install chromium || goto :err
)
if not exist "%MEDIUM_LIBRARY_DATA%\Medium-Library\library.json" goto :restore
if not exist "%MEDIUM_LIBRARY_DATA%\notes" goto :restore
if not exist "%MEDIUM_LIBRARY_DATA%\CVEs\cvelistV5.sqlite3" goto :restore
if not exist "%MEDIUM_LIBRARY_DATA%\search-index\medium.db" goto :restore
goto :start
:restore
set "HAS_LOCAL_DATA="
if exist "%MEDIUM_LIBRARY_DATA%" for /f %%F in ('dir /a /b "%MEDIUM_LIBRARY_DATA%" 2^>nul') do set "HAS_LOCAL_DATA=1"
if defined HAS_LOCAL_DATA (
  echo The local data folder exists but is incomplete: "%MEDIUM_LIBRARY_DATA%"
  echo Restore can add or replace files in this folder. Back it up before proceeding.
  choice /C YN /M "Continue with Telegram restore"
  if errorlevel 2 goto :restore_cancelled
)
echo Restoring the local fast cache from Telegram...
"%TGFS_PROJECT%\.venv\Scripts\python.exe" restore_telegram.py || goto :err
if not exist "%MEDIUM_LIBRARY_DATA%\Medium-Library\library.json" goto :restore_incomplete
if not exist "%MEDIUM_LIBRARY_DATA%\CVEs\cvelistV5.sqlite3" goto :restore_incomplete
if not exist "%MEDIUM_LIBRARY_DATA%\search-index\medium.db" goto :restore_incomplete
if not exist "%MEDIUM_LIBRARY_DATA%\notes" goto :restore_incomplete
:start
.venv\Scripts\python.exe launch_app.py
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" goto :app_failed
echo.
echo Syncing changed newsletter data to Telegram...
"%TGFS_PROJECT%\.venv\Scripts\python.exe" sync_telegram.py
if errorlevel 1 (
  echo Telegram sync failed. Your local data is still intact; rerun sync after fixing TGFS.
  exit /b 1
)
exit /b 0
:app_failed
echo The app did not finish cleanly (exit code %APP_EXIT%); skipping Telegram sync.
echo The local data remains in "%MEDIUM_LIBRARY_DATA%".
exit /b %APP_EXIT%
:tgfs_missing
echo Telegram integration is not configured: TGFS checkout not found at "%TGFS_PROJECT%\src\tgfs".
echo Clone and configure the Telegram File Storage System, then set TGFS_PROJECT to its checkout path.
echo Configure its Telegram session and encryption key there. No credentials belong in this repository.
goto :stop
:tgfs_python_missing
echo TGFS Python environment not found: "%TGFS_PROJECT%\.venv\Scripts\python.exe"
echo Create the TGFS checkout's .venv and install its requirements, then run this launcher again.
goto :stop
:restore_cancelled
echo Restore cancelled. Existing local data was left unchanged; back it up or choose another data path.
goto :stop
:restore_incomplete
echo Restore did not create the required library, CVE, search-index, and notes data.
echo Check TGFS_NEWSLETTER_PATH and the remote archive. The app was not started.
goto :stop
:err
:stop
echo Setup or Telegram integration failed. See the messages above, correct the paths/configuration, and retry.
pause
exit /b 1
