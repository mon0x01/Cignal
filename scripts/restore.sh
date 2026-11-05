#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 data/backups/cignal-backup-YYYYMMDDTHHMMSSZ" >&2
  exit 2
fi
BACKUP_DIR="$(realpath "$1")"
for required in database.dump evidence.tar.gz SHA256SUMS.txt manifest.json; do
  [[ -f "$BACKUP_DIR/$required" ]] || { echo "Missing $required" >&2; exit 1; }
done
(
  cd "$BACKUP_DIR"
  sha256sum -c SHA256SUMS.txt
)

if [[ "${RESTORE_CONFIRM:-}" != "YES" ]]; then
  echo "This overwrites the current Cignal database and evidence volume." >&2
  echo "After verifying the backup, rerun with RESTORE_CONFIRM=YES." >&2
  exit 1
fi

# Stop writers and the API while restoring. The database container remains available.
docker compose stop frontend worker scheduler api
docker compose exec -T db sh -c \
  'pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  < "$BACKUP_DIR/database.dump"
# Restore evidence into the persistent evidence volume via a one-off API container.
docker compose run --rm --no-deps api sh -c \
  'find /data/evidence -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf - -C /data/evidence' \
  < "$BACKUP_DIR/evidence.tar.gz"
# New-format backups also carry the trained model artifacts; old backups remain restorable.
if [[ -f "$BACKUP_DIR/models.tar.gz" ]]; then
  docker compose run --rm --no-deps api sh -c \
    'find /data/models -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf - -C /data/models' \
    < "$BACKUP_DIR/models.tar.gz"
  echo "Trained model artifacts restored."
else
  echo "No model archive in this legacy backup; current model volume was left unchanged."
fi
docker compose start api frontend worker scheduler

echo "Restore complete. Check /api/health and review the latest audit records."
