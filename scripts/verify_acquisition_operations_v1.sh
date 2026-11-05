#!/usr/bin/env bash
# Non-destructive host gate for Cignal Acquisition Operations Engine v1.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
compose() { sudo docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" "$@"; }

command -v docker >/dev/null 2>&1 || fail "Docker CLI unavailable."
command -v curl >/dev/null 2>&1 || fail "curl unavailable."
[[ -f .env ]] || fail ".env missing."
sudo -v

printf '\n========== 1/8 · RELEASE CONTRACTS =========='
printf '\n'
for file in \
  app/services/acquisition_schedule.py \
  migrations/versions/0014_acquisition_source_scheduler.py \
  tests/test_acquisition_schedule.py \
  tests/test_acquisition_schedule_migration.py \
  docs/ACQUISITION_OPERATIONS_V1.md; do
  [[ -s "$file" ]] || fail "Required file missing: $file"
done
grep -Fq 'class AcquisitionSourceState' app/db/models.py || fail "Durable source state model missing."
grep -Fq 'claim_due_sources' app/scheduler.py || fail "Per-source claim scheduler missing."
grep -Fq 'mark_source_started' app/worker.py || fail "Worker lease validation missing."
grep -Fq '== selected_source' app/worker.py || fail "Source-scoped ingestion filter missing."
grep -Fq 'source_schedule_status' app/api/main.py || fail "Schedule status API missing."
grep -Fq 'cignal-ingestion-source-' app/services/events.py || fail "Deterministic source job IDs missing."
printf 'Source scheduling contracts: PASS\n'

printf '\n========== 2/8 · COMPOSE + SIX SERVICES =========='
printf '\n'
compose config --quiet
compose ps
running="$(compose ps --status running --services)"
for service in api db frontend redis worker scheduler; do
  grep -Fxq "$service" <<<"$running" || fail "Required service $service is not running."
done
printf 'Six services running: PASS\n'

printf '\n========== 3/8 · COMPILE + FULL TEST + SECURITY =========='
printf '\n'
quality="$(compose exec -T --interactive=false api sh -lc '
  set -eu
  python -m compileall -q app tests migrations scripts
  echo "Python compilation: PASS"
  output="$(python -m pytest -q 2>&1)" || { printf "%s\\n" "$output"; exit 1; }
  printf "%s\\n" "$output"
  passed="$(printf "%s\\n" "$output" | grep -Eo "[0-9]+ passed" | tail -n1 | cut -d" " -f1)"
  test -n "$passed"
  test "$passed" -ge 230 || { echo "Expected at least 230 tests; found $passed."; exit 1; }
  ruff check app tests migrations scripts
  bandit -q -r app -lll
  echo "Ruff: PASS; Bandit high-severity scan: PASS"
' 2>&1)" || { printf '%s\n' "$quality"; fail "Compile/test/static-security gate failed."; }
printf '%s\n' "$quality"

printf '\n========== 4/8 · MIGRATION + DATA PRESERVATION =========='
printf '\n'
current="$(compose exec -T --interactive=false api alembic current 2>&1)"
printf '%s\n' "$current"
grep -Fq '0014_acq_source_sched' <<<"$current" || fail "Migration 0014 is not current."
compose exec -T --interactive=false api python - <<'PY'
from sqlalchemy import inspect
from app.db.session import engine
required_tables = {
    "acquisition_source_states", "integration_runs",
    "event_hypotheses", "event_hypothesis_reports", "event_discovery_runs",
    "report_semantic_embeddings", "event_hypothesis_snapshots", "source_reliability_profiles",
    "research_datasets", "event_pair_labels", "research_dataset_versions", "research_dataset_version_items",
}
existing = set(inspect(engine).get_table_names())
missing = sorted(required_tables - existing)
if missing:
    raise SystemExit(f"Required database tables are missing: {missing}")
with engine.connect() as connection:
    rows = connection.exec_driver_sql("SELECT count(*) FROM acquisition_source_states").scalar_one()
print(f"Durable source schedule rows: {rows}")
print(f"Integration/event/research schema tables: {len(required_tables)}/{len(required_tables)} PASS")
PY
printf '\n========== 5/8 · API ROUTES + SCHEDULER WIRING =========='
printf '\n'
compose exec -T --interactive=false api python - <<'PY'
from app.api.main import app
from app.services.acquisition_schedule import claim_due_sources, source_schedule_status
from app.scheduler import _schedule_due_source_jobs
paths = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
required = {
    ("/api/integrations/schedules", "GET"),
    ("/api/integrations/ingest/{source_type}", "POST"),
}
missing = required - paths
if missing:
    raise SystemExit(f"Missing acquisition operations routes: {sorted(missing)}")
print("Schedule and source-specific ingestion APIs: PASS")
print("Durable scheduler service imports: PASS")
print("Source-level dispatcher imports: PASS")
PY

printf '\n========== 6/8 · API + FRONTEND HEALTH =========='
printf '\n'
health=""
for attempt in $(seq 1 30); do
  if health="$(curl --silent --show-error --fail --max-time 4 http://127.0.0.1:8000/api/health 2>/dev/null)"; then break; fi
  sleep 2
done
[[ -n "$health" ]] || fail "API health endpoint unavailable."
printf '%s\n' "$health"
grep -Fq '"status":"ok"' <<<"$health" || fail "API unhealthy."
grep -Fq '"database":"ok"' <<<"$health" || fail "Database unhealthy."
grep -Fq '"redis":"ok"' <<<"$health" || fail "Redis unhealthy."
frontend="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:8080/ || true)"
printf 'Frontend HTTP: %s\n' "$frontend"
[[ "$frontend" == "200" ]] || fail "Frontend unavailable."

printf '\n========== 7/8 · STORAGE VOLUMES =========='
printf '\n'
compose exec -T --interactive=false api sh -lc 'test -w /data/evidence && echo "Evidence storage: writable"'
compose exec -T --interactive=false api sh -lc 'test -w /data/models && echo "Model storage: writable"'
compose exec -T --interactive=false db sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
compose exec -T --interactive=false redis sh -lc 'redis-cli -a "$REDISCLI_AUTH" ping | grep -q PONG && echo "Redis persistence service: healthy"'

printf '\n========== 8/8 · FINAL STATUS =========='
printf '\n'
compose ps
printf '\nALL CIGNAL ACQUISITION OPERATIONS V1 GATES PASSED.\n'
printf 'No live ingestion was triggered by this verification script.\n'
printf 'Existing reports, evidence, Redis and model volumes were not reset.\n'
