#!/bin/sh

# Durable Linux entrypoint used by the .desktop launcher.
cd "$(dirname "$0")" || {
    printf 'Could not enter the Tumblr Scraper folder.\n'
    printf 'Press Enter to close this terminal. '
    read -r _ || true
    exit 1
}

status=0
if command -v python3 >/dev/null 2>&1; then
    python3 "Android Tumblr-Scraper.py"
    status=$?
else
    printf 'Python 3 was not found. Install Python 3, then run this launcher again.\n'
    status=1
fi

printf '\n'
if [ "$status" -ne 0 ]; then
    printf 'The launcher exited with status %s. Saved work was not removed.\n' "$status"
fi
printf 'Press Enter to close this terminal. '
read -r _ || true
exit "$status"
