@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0partykeys.exe" (
  echo Extract the full ZIP before opening Practice lights.cmd.
  pause
  exit /b 1
)
"%~dp0partykeys.exe" gui --config "%~dp0config.json"
if errorlevel 1 pause
