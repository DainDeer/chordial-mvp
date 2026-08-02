#!/bin/sh
# thin cron-friendly wrapper around scripts/pg_backup.py - the python side
# owns connection parsing (passwords with url-special characters break
# libpq's url parser; sqlalchemy's make_url, the parser the app itself
# connects with, does not). schedule e.g.:
#   crontab -e  ->  30 3 * * * POETRY=/home/dain/.local/bin/poetry /home/dain/chordial-mvp/scripts/pg_backup.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "${POETRY:-poetry}" run python scripts/pg_backup.py "$@"
