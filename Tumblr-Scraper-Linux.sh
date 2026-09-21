#!/bin/sh

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || {
    printf 'Tumblr Scraper: could not locate the launcher directory.\n' >&2
    printf 'Press Enter to close this terminal. '
    read -r _ || true
    exit 1
}

cd "$PROJECT_ROOT" || {
    printf 'Tumblr Scraper: could not enter %s.\n' "$PROJECT_ROOT" >&2
    printf 'Press Enter to close this terminal. '
    read -r _ || true
    exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
    printf 'Tumblr Scraper: Python 3 was not found.\n' >&2
    printf 'Install Python 3, then run this launcher again.\n' >&2
    printf 'Press Enter to close this terminal. '
    read -r _ || true
    exit 1
fi

exec python3 "$PROJECT_ROOT/bootstrap.py" browser
