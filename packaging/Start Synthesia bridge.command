#!/bin/sh
cd "$(dirname "$0")" || exit 1
printf '%s\n' 'PartyKeys bridge — keyboard firmware v9 required.'
printf '%s\n' 'This launcher creates the Synthesia Lights MIDI destination automatically.'
printf '%s\n' 'In Synthesia, select that Music Output and set Key Lights to Finger-based channel.'
printf '%s\n' 'Turn off ordinary notes, background, percussion and metronome on that output.'
printf '%s\n' 'Press Ctrl+C to stop and clear lights.'
./partykeys bridge --virtual-input --input "Synthesia Lights" --output PartyKeys --keyboard-input PartyKeys --palette-interval-ms 10 "$@"
status=$?
printf '%s\n' 'Press Return to close this window.'
read -r answer
exit "$status"
