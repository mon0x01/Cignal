#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and configure secrets first." >&2
  exit 1
fi

mkdir -p data/backups
# The worker runs as UID 10001 inside the container; allow it to write this bind mount.
if [[ "$(stat -c '%u' data/backups)" != "10001" ]]; then
  echo "data/backups must be writable by container UID 10001." >&2
  echo "Run: sudo chown -R 10001:10001 data/backups" >&2
  exit 1
fi

docker compose exec -T worker python -c 'from app.worker import run_scheduled_backup; print(run_scheduled_backup())'
latest="$(find data/backups -maxdepth 1 -type d -name 'cignal-backup-*' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
if [[ -z "$latest" ]]; then
  echo "Backup command returned without a backup directory." >&2
  exit 1
fi
(
  cd "$latest"
  sha256sum -c SHA256SUMS.txt
)
echo "Backup verified: $latest"
