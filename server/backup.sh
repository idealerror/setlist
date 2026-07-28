#!/usr/bin/env bash
# Nightly Postgres backup (spec 10, phase 7).
#
# Run from the host via cron, next to docker-compose.yml:
#   15 6 * * *  cd /srv/venue-setlist/server && ./backup.sh >> backup.log 2>&1
#
# Dumps are custom-format (-Fc), which restores selectively with pg_restore and
# compresses far better than plain SQL.

set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

RETAIN_DAYS="${RETAIN_DAYS:-30}"
DB_USER="${POSTGRES_USER:-setlist}"
DB_NAME="${POSTGRES_DB:-setlist}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/backups/setlist-${STAMP}.dump"

mkdir -p ./backups

docker compose exec -T db \
    pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -f "$OUT"

# Verify the dump is readable before trusting it. A backup that has never been
# listed is not a backup.
docker compose exec -T db pg_restore --list "$OUT" > /dev/null
echo "$(date -u +%FT%TZ) ok ${OUT} ($(du -h "./backups/setlist-${STAMP}.dump" | cut -f1))"

find ./backups -name 'setlist-*.dump' -mtime "+${RETAIN_DAYS}" -delete
