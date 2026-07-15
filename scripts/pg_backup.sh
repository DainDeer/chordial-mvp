#!/bin/sh
# nightly postgres backup (NATIVE_MIGRATION_PLAN §2.6): pg_dump custom-format
# dumps into backups/, keeping the newest 14. schedule via launchd/cron, e.g.:
#   crontab -e   ->   30 3 * * * /Users/dain/Code/chordial-mvp/scripts/pg_backup.sh
# restore rehearsal (the §2.5 preflight gate):
#   createdb restore_test && pg_restore -d restore_test backups/chordial-<ts>.dump
set -eu

DB="${1:-chordial}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
KEEP=14

mkdir -p "$DIR"
pg_dump -Fc -d "$DB" -f "$DIR/${DB}-$(date +%Y%m%d-%H%M%S).dump"

# rotate: delete all but the newest $KEEP dumps for this db
ls -t "$DIR/${DB}"-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -- "$old"
done
