#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 data/backups/cignal-backup-YYYYMMDDTHHMMSSZ" >&2
  exit 2
fi
BACKUP_DIR="$(realpath "$1")"
(
  cd "$BACKUP_DIR"
  sha256sum -c SHA256SUMS.txt
  tar -tzf evidence.tar.gz >/dev/null
  if [[ -f models.tar.gz ]]; then tar -tzf models.tar.gz >/dev/null; fi
)
POSTGRES_USER="$(docker compose exec -T db printenv POSTGRES_USER | tr -d '\r')"
TEST_DB="cignal_restore_test_$(date +%s)"
cleanup() { docker compose exec -T db dropdb --if-exists -U "$POSTGRES_USER" "$TEST_DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker compose exec -T db createdb -U "$POSTGRES_USER" "$TEST_DB"
docker compose exec -T db pg_restore \
  --username "$POSTGRES_USER" --dbname "$TEST_DB" \
  --no-owner --no-privileges < "$BACKUP_DIR/database.dump"
result="$(docker compose exec -T db psql -At -U "$POSTGRES_USER" -d "$TEST_DB" -c 'SELECT 1')"
[[ "$result" == "1" ]]
echo "Database restore test passed for $TEST_DB; evidence archive and checksums also validated."
