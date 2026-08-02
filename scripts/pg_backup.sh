#!/bin/sh
# nightly postgres backup (NATIVE_MIGRATION_PLAN §2.6): pg_dump custom-format
# dumps into backups/, keeping the newest 14. schedule via launchd/cron, e.g.:
#   crontab -e   ->   30 3 * * * /home/dain/chordial-mvp/scripts/pg_backup.sh
# restore rehearsal (the §2.5 preflight gate):
#   createdb restore_test && pg_restore -d restore_test backups/chordial-<ts>.dump
#
# connection: $1 if given (name or libpq url), else DATABASE_URL from the
# repo .env - the SAME dsn the app runs on, so role/password/host are right
# by construction (a bare db name relies on peer auth matching the os user,
# which holds on the dev mac but not on the server, where the only role is
# 'chordial'). sqlalchemy's +psycopg driver suffix is stripped for libpq.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DSN="${1:-}"
if [ -z "$DSN" ] && [ -f "$ROOT/.env" ]; then
    DSN="$(grep -E '^DATABASE_URL=' "$ROOT/.env" | tail -n 1 \
        | cut -d= -f2- | sed 's/+psycopg//')"
fi
DSN="${DSN:-chordial}"

# the dump filename keeps the plain db name even when DSN is a url
NAME="${DSN##*/}"
NAME="${NAME%%\?*}"
NAME="${NAME:-chordial}"

DIR="$ROOT/backups"
KEEP=14

mkdir -p "$DIR"
pg_dump -Fc -d "$DSN" -f "$DIR/${NAME}-$(date +%Y%m%d-%H%M%S).dump"

# rotate: delete all but the newest $KEEP dumps for this db
ls -t "$DIR/${NAME}"-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -- "$old"
done
