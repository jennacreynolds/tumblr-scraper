#!/bin/sh

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || {
    printf 'Tumblr Scraper: could not locate the launcher directory.\n' >&2
    printf 'Press Enter to close this terminal. '
    read -r _ || true
    exit 1
}

cd "$PROJECT_ROOT" || exit 1

status=0
if command -v python3 >/dev/null 2>&1; then
    python3 "$PROJECT_ROOT/bootstrap.py" browser
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
