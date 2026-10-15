#!/bin/sh
cd "$(dirname "$0")" || exit 1
printf '%s\n' 'PartyKeys bridge — keyboard firmware v9 required.'
printf '%s\n' 'Enable the Synthesia Lights IAC bus and select it for Synthesia key lights.'
printf '%s\n' 'Press Ctrl+C to stop and clear lights.'
./partykeys bridge --input "IAC Driver Synthesia Lights" --output PartyKeys --pressed-input PartyKeys --palette-interval-ms 10
status=$?
printf '%s\n' 'Press Return to close this window.'
read -r answer
exit "$status"
