#!/usr/bin/env bash
# Non-destructive host quality gate for the Data Acquisition Engine v1 overlay.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
compose() { sudo docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" "$@"; }

command -v docker >/dev/null 2>&1 || fail "Docker CLI is unavailable."
command -v curl >/dev/null 2>&1 || fail "curl is unavailable."
sudo -v
[[ -f .env ]] || fail ".env is missing; verification will not create it."

printf '\n========== 1/8 · RELEASE FILES + SOURCE CONTRACTS ==========\n'
for file in \
  app/integrations/source_catalog.py app/integrations/eonet.py app/integrations/sachet_cap.py \
  migrations/versions/0013_source_provenance.py \
  tests/test_data_acquisition.py tests/test_source_provenance.py tests/test_source_provenance_migration.py \
  docs/DATA_ACQUISITION_V1.md; do
  [[ -s "$file" ]] || fail "Required data-acquisition release file missing: $file"
done
grep -Fq 'class NASAEONETEvents' app/integrations/eonet.py || fail "NASA EONET connector is missing."
grep -Fq 'class SachetCAPFeed' app/integrations/sachet_cap.py || fail "SACHET CAP connector is missing."
grep -Fq 'If-None-Match' app/integrations/sachet_cap.py || fail "SACHET conditional ETag request handling is missing."
grep -Fq 'source_metadata' app/db/models.py || fail "Report source provenance model field is missing."
grep -Fq 'source_catalog_versions' app/services/research_datasets.py || fail "Research dataset source-catalogue manifest metadata is missing."
grep -Fq 'source_metadata' app/api/main.py || fail "Report API provenance output is missing."
printf 'Release files and source contracts: PASS\n'

printf '\n========== 2/8 · COMPOSE CONFIGURATION + SERVICE AVAILABILITY ==========\n'
compose config --quiet
compose ps
running_services="$(compose ps --status running --services)"
for service in api db frontend redis worker scheduler; do
  grep -Fxq "$service" <<<"$running_services" || fail "Compose service '$service' is not running."
done
printf 'Six required services running: PASS\n'

printf '\n========== 3/8 · PYTHON COMPILE + FULL TEST SUITE + STATIC SECURITY ==========\n'
quality_output="$(compose exec -T --interactive=false api sh -lc '
  set -eu
  python -m compileall -q app tests migrations scripts
  echo "Python compilation: PASS"
  test_output="$(python -m pytest -q 2>&1)" || { printf "%s\\n" "$test_output"; exit 1; }
  printf "%s\\n" "$test_output"
  passed="$(printf "%s\\n" "$test_output" | grep -Eo "[0-9]+ passed" | tail -n1 | cut -d" " -f1)"
  test -n "$passed" || { echo "Pytest result did not report passed tests."; exit 1; }
  test "$passed" -ge 217 || { echo "Only $passed tests discovered; expected at least 217."; exit 1; }
  ruff check app tests migrations scripts
  bandit -q -r app -lll
  echo "Ruff: PASS; Bandit high-severity scan: PASS"
' 2>&1)" || { printf '%s\n' "$quality_output"; fail "Container compile/test/static-security gate failed."; }
printf '%s\n' "$quality_output"

printf '\n========== 4/8 · DATABASE MIGRATION + DATA PRESERVATION ==========\n'
migration="$(compose exec -T --interactive=false api alembic current 2>&1)"
printf '%s\n' "$migration"
grep -Fq '0013_source_provenance' <<<"$migration" || fail "Migration 0013_source_provenance is not current."
source_column="$(compose exec -T --interactive=false db sh -lc \
  'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -c "SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='\''reports'\'' AND column_name='\''source_metadata'\''"')"
printf 'reports.source_metadata type: %s\n' "$source_column"
[[ "$source_column" == "json" || "$source_column" == "jsonb" ]] || fail "reports.source_metadata is not JSON/JSONB."
null_rows="$(compose exec -T --interactive=false db sh -lc \
  'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -c "SELECT count(*) FROM reports WHERE source_metadata IS NULL"')"
printf 'Reports with NULL source metadata: %s\n' "$null_rows"
[[ "$null_rows" == "0" ]] || fail "Existing reports have NULL source metadata."

for table in \
  event_hypotheses event_hypothesis_reports event_discovery_runs \
  report_semantic_embeddings event_hypothesis_snapshots source_reliability_profiles \
  research_datasets event_pair_labels research_dataset_versions research_dataset_version_items; do
  found="$(compose exec -T --interactive=false db sh -lc \
    "psql -X -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atq -c \"SELECT to_regclass('public.$table') IS NOT NULL\"")"
  [[ "$found" == "t" ]] || fail "Previously installed intelligence/research table is missing: $table"
done
printf 'Event Intelligence v1/v2 + Research Data v1 tables: 10/10 preserved\n'

printf '\n========== 5/8 · CONNECTOR CATALOGUE + API ROUTES ==========\n'
compose exec -T --interactive=false api python -c '
from app.api.main import app
from app.integrations.registry import connector_statuses
from app.integrations.source_catalog import source_descriptor_payload
paths = {route.path for route in app.routes}
required = {"/api/integrations", "/api/integrations/health", "/api/integrations/runs", "/api/integrations/ingest"}
missing = required - paths
if missing:
    raise SystemExit(f"Missing integration API routes: {sorted(missing)}")
statuses = connector_statuses()
eonet = next((item for item in statuses if item["source_type"] == "nasa_eonet"), None)
if not eonet or eonet.get("source_governance", {}).get("license_status") != "source_dependent_metadata":
    raise SystemExit("NASA EONET connector/source-governance descriptor is missing.")
sachet = source_descriptor_payload("sachet_cap")
if sachet.get("license_status") != "official_feed_terms_review_required":
    raise SystemExit("SACHET source-governance descriptor is missing.")
print("Integration API routes: PASS")
print("NASA EONET connector/catalogue: PASS")
print("SACHET CAP source catalogue: PASS")
print(f"Registered connectors: {len(statuses)}")
'


printf '\n========== 6/8 · API + FRONTEND HEALTH ==========\n'
health=""
for attempt in $(seq 1 30); do
  if health="$(curl --silent --show-error --fail --max-time 4 http://127.0.0.1:8000/api/health 2>/dev/null)"; then break; fi
  sleep 2
done
[[ -n "$health" ]] || fail "API health endpoint did not return HTTP 200."
printf '%s\n' "$health"
grep -Fq '"status":"ok"' <<<"$health" || fail "Application health is not ok."
grep -Fq '"database":"ok"' <<<"$health" || fail "Database health is not ok."
grep -Fq '"redis":"ok"' <<<"$health" || fail "Redis health is not ok."
frontend_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:8080/ || true)"
printf 'Frontend HTTP: %s\n' "$frontend_code"
[[ "$frontend_code" == "200" ]] || fail "Frontend did not return HTTP 200."

printf '\n========== 7/8 · STORAGE + MODEL VOLUME ACCESS ==========\n'
compose exec -T --interactive=false api sh -lc 'test -d /data/evidence && test -w /data/evidence && echo "API evidence storage: writable"'
compose exec -T --interactive=false api sh -lc 'test -d /data/models && test -w /data/models && echo "API model volume: writable"'
compose exec -T --interactive=false worker sh -lc 'test -d /data/models && test -r /data/models && echo "Worker model volume: readable"'

printf '\n========== 8/8 · FINAL SERVICE STATE ==========\n'
compose ps
printf '\nALL CIGNAL DATA ACQUISITION ENGINE V1 QUALITY GATES PASSED.\n'
printf 'No ingestion was triggered. Database, evidence, Redis and model volumes were not deleted or reset.\n'
