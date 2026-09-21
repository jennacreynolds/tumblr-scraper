#!/bin/sh

cd "$(dirname "$0")" || exit 1

status=0
if command -v python3 >/dev/null 2>&1; then
    python3 "Run Tumblr Backup.py"
    status=$?
else
    echo "Python 3 was not found. Install Python 3 and try again."
    status=1
fi

echo
if [ "$status" -ne 0 ]; then
    printf 'The launcher exited with status %s. Saved work was not removed.\n' "$status"
fi
printf 'Press Enter to close this window. '
read -r _ || true
exit "$status"
