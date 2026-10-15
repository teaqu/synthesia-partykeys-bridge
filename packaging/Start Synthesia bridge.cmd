@echo off
cd /d "%~dp0"
echo PartyKeys bridge - keyboard firmware v9 required.
echo Create a loopMIDI port named Synthesia Lights and enable it in Synthesia's Key Lights settings.
echo Press Ctrl+C to stop and clear lights.
"%~dp0partykeys.exe" bridge --input "Synthesia Lights" --no-press-feedback --palette-interval-ms 10
pause
