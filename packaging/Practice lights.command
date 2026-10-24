#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec ./partykeys gui --config "$(pwd)/config.json"
