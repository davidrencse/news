@echo off
cd /d "%~dp0"
set "MEDIUM_LIBRARY_STORAGE=telegram"
set "PYTHONDONTWRITEBYTECODE=1"
rem Paywalled/Cloudflare-blocked stories fall back to a self-hosted Freedium (see docs\freedium-selfhost.md).
rem Override FREEDIUM_BASE to point elsewhere, or FREEDIUM_MIRRORS for a custom ordered list.
if not defined FREEDIUM_BASE set "FREEDIUM_BASE=http://localhost:6752"
if not defined TGFS_PROJECT set "TGFS_PROJECT=%USERPROFILE%\Desktop\telegram-file-storage-system"
if not exist "%TGFS_PROJECT%\src\tgfs" goto :tgfs_missing
if not exist "%TGFS_PROJECT%\.venv\Scripts\python.exe" goto :tgfs_missing
if not exist .venv (
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
echo Opening the library directly from Telegram. No local data cache is restored.
.venv\Scripts\python.exe -B launch_app.py
exit /b %ERRORLEVEL%
:tgfs_missing
echo Configure the Telegram File Storage System and set TGFS_PROJECT to its checkout.
echo Its login session and encryption key must already be configured. Local fallback is disabled.
:err
pause
exit /b 1
