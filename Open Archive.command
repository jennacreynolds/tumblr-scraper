#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 open_archive.py "$@"
status=$?
printf '\nPress Enter to close this window. '
read -r _ || true
exit "$status"
