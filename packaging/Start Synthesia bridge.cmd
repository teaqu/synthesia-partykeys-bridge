@echo off
setlocal
if not exist "%~dp0partykeys.exe" goto extract_first
if not exist "%~dp0native-midi-helper\PartyKeys.Midi.exe" goto extract_first
if not exist "%~dp0config.json" goto extract_first
cd /d "%~dp0"
echo PartyKeys bridge - keyboard firmware v9 required.
echo Preparing Synthesia Lights automatically using the Windows MIDI API (preview).
echo In Synthesia, select Synthesia Lights as a music output and enable Key Lights.
echo Press Ctrl+C to stop and clear lights.
"%~dp0partykeys.exe" bridge --config "%~dp0config.json"
set "bridge_status=%errorlevel%"
pause
exit /b %bridge_status%

:extract_first
echo.
echo ERROR: Extract the ZIP first.
echo Right-click the downloaded ZIP and choose Extract All.
echo Then open Start Synthesia bridge.cmd from the extracted folder.
echo Keep partykeys.exe, config.json and the native-midi-helper folder together.
echo.
pause
exit /b 1
