@echo off
REM Windows launcher - double-click this file.
REM First run installs everything (a few hundred MB); later runs start instantly.
cd /d "%~dp0"

echo ================================================
echo    AI Web Tester
echo ================================================

REM Make sure uv (the installer/runtime manager) is available.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo Installing the runtime manager ^(uv^)... one-time.
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

REM One-time install of Python deps + Chromium.
if not exist ".venv" (
  echo First-time setup - installing Python, dependencies and a browser.
  echo This can take several minutes. Please wait...
  uv venv --python 3.12 || goto :fail
  uv pip install -r requirements.txt || goto :fail
  .venv\Scripts\python -m browser_use install || goto :fail
)

REM Create a local settings file on first run (never overwrites an existing one).
if not exist ".env" copy ".env.example" ".env" >nul

echo Starting... your browser will open automatically.
.venv\Scripts\python app.py
echo Server stopped.
pause
exit /b 0

:fail
echo.
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
