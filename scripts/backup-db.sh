#!/usr/bin/env bash
#
# Dump the bundled Postgres: the carry history AND Grafana's own state.
#
# Both live in one instance deliberately - that is why Grafana is configured with
# GF_DATABASE_TYPE=postgres rather than its default SQLite - so one dump covers
# the measurements and the dashboards, users and alert rules that interpret them.
# A backup of one without the other restores a box that has the numbers and no
# way to read them, or the other way round.
#
# Written for cron: quiet on success, loud on failure, and it never leaves a
# half-written file behind under the name of a good one.
#
# RESTORE - see the end of this file. Read it before you need it.

set -euo pipefail

REPO="${REPO:-$HOME/carry_monitor}"
OUT="${OUT:-$HOME/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
CONTAINER="${CONTAINER:-carry_timescale}"

cd "$REPO"

# Values are quoted in .env, so read them rather than sourcing the file.
env_val() { grep "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"'\'' '; }
USER_DB=$(env_val TS_DB_USER)
NAME_DB=$(env_val TS_DB_NAME)

if [ -z "$USER_DB" ] || [ -z "$NAME_DB" ]; then
  echo "backup: could not read TS_DB_USER / TS_DB_NAME from $REPO/.env" >&2
  exit 1
fi

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "backup: $CONTAINER is not running - nothing dumped" >&2
  exit 1
fi

mkdir -p "$OUT"
chmod 700 "$OUT"
STAMP=$(date -u +%Y%m%d-%H%M%S)

# Write to .part and rename only on success. A truncated dump that carries a
# good name is worse than no dump, because it is the one you reach for.
dump() {                       # $1 = label, $2... = command inside the container
  local label="$1"; shift
  local final="$OUT/${STAMP}-${label}.sql.gz"
  local part="${final}.part"

  if docker exec "$CONTAINER" "$@" 2>/dev/null | gzip -9 > "$part"; then
    # gzip of an empty stream is still ~20 bytes, so check for substance.
    if [ "$(stat -c%s "$part")" -lt 200 ]; then
      echo "backup: $label dump is suspiciously small, keeping as .part" >&2
      return 1
    fi
    mv "$part" "$final"
    chmod 600 "$final"
    echo "  $(basename "$final")  $(du -h "$final" | cut -f1)"
  else
    echo "backup: $label dump FAILED" >&2
    rm -f "$part"
    return 1
  fi
}

echo "backup $STAMP"
dump globals pg_dumpall -U "$USER_DB" --globals-only
dump "$NAME_DB" pg_dump -U "$USER_DB" -d "$NAME_DB"
dump grafana   pg_dump -U "$USER_DB" -d grafana

# Prune. Only whole dated sets are removed, and .part files go with them.
find "$OUT" -name '*.sql.gz' -mtime "+$KEEP_DAYS" -print -delete | sed 's/^/  pruned /'
find "$OUT" -name '*.part' -mtime +1 -delete

echo "  total: $(du -sh "$OUT" | cut -f1) in $OUT"

# ---------------------------------------------------------------- RESTORE ----
#
# TimescaleDB needs bracketing calls around the restore; a plain psql -f of these
# dumps into a database with the extension already loaded will fail on the
# hypertable metadata. Into a FRESH database:
#
#   createdb -U <user> restored
#   psql -U <user> -d restored -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
#   psql -U <user> -d restored -c "SELECT timescaledb_pre_restore();"
#   zcat 20260909-*-carry_monitor.sql.gz | psql -U <user> -d restored
#   psql -U <user> -d restored -c "SELECT timescaledb_post_restore();"
#
# The grafana dump has no hypertables and restores with plain psql.
#
# The globals dump carries the roles. It is only needed when restoring onto a new
# instance, not when restoring a database beside an existing one.
#
# THESE DUMPS ARE ON THE SAME DISK AS THE DATABASE. That covers a bad migration,
# a dropped table or a mistaken `down -v`. It does NOT cover losing the box or the
# volume. Copy them off if the dashboards become worth more than the effort.
