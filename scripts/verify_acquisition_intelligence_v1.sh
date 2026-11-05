#!/usr/bin/env bash
# Complete verification for Cignal acquisition, intelligence and incident operations.
# This gate does not start live acquisition and never resets persistent volumes.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
compose() { "${DOCKER[@]}" compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" "$@"; }
command -v docker >/dev/null 2>&1 || fail "Docker CLI unavailable."
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  fail "Docker daemon unavailable."
fi
[[ -f .env ]] || fail ".env is missing."

printf '\n========== 1/9 · RELEASE CONTRACTS =========='
printf '\n'
for file in \
  app/services/acquisition_archive.py \
  app/services/active_learning.py \
  app/integrations/usgs.py \
  app/integrations/eonet.py \
  app/worker.py app/scheduler.py app/api/main.py app/api/schemas.py app/db/models.py \
  migrations/versions/0015_acq_archive_learning.py migrations/versions/0016_incident_operations.py \
  app/api/incident_operations.py app/api/response_plans.py \
  app/services/response_playbooks.py app/services/response_orchestration.py \
  migrations/versions/0017_response_plan_orchestration.py migrations/versions/0018_durable_job_outbox.py migrations/versions/0019_continuous_event_discovery.py migrations/versions/0020_event_evolution_replay.py migrations/versions/0021_job_execution_reliability.py \
  app/services/job_outbox.py app/services/job_execution.py app/services/event_discovery_schedule.py app/services/event_replay.py docs/EVENT_EVOLUTION_REPLAY_V1.md docs/BACKGROUND_JOB_OUTBOX_V1.md docs/DISTRIBUTED_JOB_RELIABILITY_V1.md docs/CONTINUOUS_EVENT_DISCOVERY_V1.md tests/test_job_outbox.py tests/test_job_execution.py tests/test_worker_execution.py tests/test_continuous_event_discovery.py tests/test_event_replay.py docs/INCIDENT_OPERATIONS_V1.md \
  docs/INCIDENT_RESPONSE_ORCHESTRATION_V1.md tests/test_response_orchestration.py \
  tests/test_api_workflow.py tests/test_migrations.py \
  scripts/run_historical_backfill.py scripts/smoke_external_sources.py \
  scripts/verify_live_acquisition_e2e.py scripts/mine_event_pair_candidates.py \
  scripts/evaluate_event_pair_model.py docs/ACQUISITION_INTELLIGENCE_V1.md; do
  [[ -s "$file" ]] || fail "Missing release file: $file"
done
grep -Fq 'def capture_observation(' app/services/acquisition_archive.py || fail "Immutable observation capture missing."
grep -Fq 'def run_acquisition_backfill(' app/worker.py || fail "Historical backfill worker missing."
grep -Fq 'def mine_event_pair_candidates(' app/services/active_learning.py || fail "Active-learning candidate miner missing."
grep -Fq 'candidate_review_only_human_required' app/services/active_learning.py || grep -Fq 'unlabelled_candidate_only_human_review_required' app/services/active_learning.py || fail "Human-review-only policy missing."
grep -Fq 'validate_external_evaluation_groups' scripts/evaluate_event_pair_model.py || fail "External holdout leakage validation missing."
grep -Fq 'artifact_sha256' scripts/evaluate_event_pair_model.py || fail "Model artifact fingerprint missing."
grep -Fq 'schedule_continuous_event_discovery' app/services/event_discovery_schedule.py || fail "Continuous event discovery scheduler missing."
grep -Fq 'enqueue_event_discovery' app/services/job_outbox.py || fail "Durable event discovery outbox integration missing."
grep -Fq 'enqueue_event_replay' app/services/job_outbox.py || fail "Durable event replay outbox integration missing."
grep -Fq 'def replay_event_history' app/worker.py || fail "Event replay worker entry point missing."
grep -Fq 'baseline_manifest_sha256' app/services/event_replay.py || fail "Frozen replay comparison-baseline integrity missing."
grep -Fq 'use_supervised_model=False' app/services/event_replay.py || fail "Deterministic replay scoring mode missing."
grep -Fq 'execute_outbox_job' app/worker.py || fail "Outbox execution wrapper missing."
grep -Fq 'recover_expired_executions' app/scheduler.py || fail "Expired worker execution recovery missing."
grep -Fq 'row.last_rq_job_id != str(rq_job_id)' app/services/job_execution.py || fail "RQ delivery identity fence missing."
grep -Fq 'row.last_rq_job_id = None' app/services/job_execution.py || fail "Failed/expired execution delivery retirement missing."
grep -Fq 'outbox_execution_fence_busy' app/worker.py || fail "Busy execution fence does not defer duplicate RQ deliveries."
grep -Fq 'row.last_rq_job_id = None' app/services/job_outbox.py || fail "Failed enqueue delivery retirement missing."
grep -Fq 'pg_try_advisory_lock' app/worker.py || fail "Cross-process duplicate execution fence missing."
grep -Fq 'engine.dispose(close=False)' app/worker.py || fail "Fork-safe SQLAlchemy pool disposal missing."
grep -Fq 'CIGNAL_WORKER_QUEUES: cignal-priority,cignal' docker-compose.yml || fail "Primary worker queue lanes missing."
grep -Fq 'CIGNAL_WORKER_QUEUES: cignal-bulk' docker-compose.yml || fail "Bulk worker queue lane missing."
grep -Fq 'stop_grace_period: 65m' docker-compose.yml || fail "Worker graceful-shutdown budget missing."
grep -Fq 'CIGNAL_WORKER_QUEUES' app/worker.py || fail "Worker queue-lane configuration missing."
grep -Fq 'worker-bulk:' docker-compose.yml || fail "Bulk worker service missing."
grep -Fq 'continuous_event_discovery_enabled' app/core/config.py || fail "Continuous event discovery configuration missing."
echo "Release files and source contracts: PASS"

printf '\n========== 2/9 · COMPOSE CONFIGURATION =========='
printf '\n'
compose config --quiet
echo "Compose configuration: PASS"

printf '\n========== 3/9 · SEVEN REQUIRED SERVICES =========='
printf '\n'
compose ps
running="$(compose ps --status running --services)"
for service in api db frontend redis worker worker-bulk scheduler; do
  grep -Fxq "$service" <<<"$running" || fail "Service '$service' is not running."
done
echo "Seven required services running: PASS"

printf '\n========== 4/9 · COMPILE, FULL TESTS AND STATIC SECURITY =========='
printf '\n'
compose exec -T --interactive=false api sh -lc '
  set -eu
  python -m compileall -q app tests migrations scripts
  echo "Python compilation: PASS"
  test_output="$(python -m pytest -q 2>&1)" || { printf "%s\\n" "$test_output"; exit 1; }
  printf "%s\\n" "$test_output"
  passed_line="$(printf "%s\\n" "$test_output" | grep -Eo "[0-9]+ passed" | tail -n1 || true)"
  passed="${passed_line%% *}"
  test -n "$passed" || { echo "Pytest summary missing."; exit 1; }
  test "$passed" -ge 297 || { echo "Only $passed tests passed; expected at least 297."; exit 1; }
  ruff check app tests migrations scripts
  bandit -q -r app -lll
  echo "Ruff: PASS; Bandit high-severity scan: PASS"
'

printf '\n========== 5/9 · MIGRATION AND DATABASE SCHEMA =========='
printf '\n'
current="$(compose exec -T --interactive=false api alembic current 2>&1)"
printf '%s\n' "$current"
grep -Fq '0021_job_execution_reliability' <<<"$current" || fail "Expected migration 0021_job_execution_reliability is not current."
compose exec -T --interactive=false api python - <<'PY'
from sqlalchemy import inspect
from app.db.session import engine
inspector = inspect(engine)
required = {
    "acquisition_observations", "acquisition_backfill_runs", "acquisition_backfill_items",
    "pair_candidate_mining_runs", "event_pair_candidates", "model_evaluation_runs",
    "acquisition_source_states", "research_datasets", "event_pair_labels",
    "research_dataset_versions", "research_dataset_version_items",
    "event_hypotheses", "event_hypothesis_reports", "event_discovery_runs",
    "incident_actions", "incident_action_events", "incident_response_plans",
    "incident_action_dependencies", "background_job_outbox",
    "event_replay_runs", "event_replay_inputs", "event_replay_baselines", "event_replay_results",
    "background_job_execution_attempts",
}
actual = set(inspector.get_table_names())
missing = sorted(required - actual)
if missing:
    raise SystemExit(f"Missing expected tables: {missing}")
print(f"Acquisition, governance and execution-ledger tables: {len(required)}/{len(required)} present")
for table, unique_name in (
    ("acquisition_observations", "uq_acq_observation_version"),
    ("acquisition_backfill_items", "uq_acq_backfill_item_observation"),
    ("event_pair_candidates", "uq_event_pair_candidate_fingerprint"),
):
    constraints = {item.get("name") for item in inspector.get_unique_constraints(table)}
    if unique_name not in constraints:
        raise SystemExit(f"Unique constraint missing: {table}.{unique_name}")
job_outbox_constraints = {
    item.get("name") for item in inspector.get_unique_constraints("background_job_outbox")
}
if "uq_background_job_outbox_job_id" not in job_outbox_constraints:
    raise SystemExit("Deterministic outbox job-id uniqueness constraint missing")
job_outbox_indexes = {item.get("name") for item in inspector.get_indexes("background_job_outbox")}
if "ix_background_job_outbox_status_available" not in job_outbox_indexes:
    raise SystemExit("Due-job outbox dispatch index missing")
if "ix_background_job_outbox_execution_lease" not in job_outbox_indexes:
    raise SystemExit("Execution-lease recovery index missing")
job_outbox_checks = {
    item.get("name") for item in inspector.get_check_constraints("background_job_outbox")
}
if not {
    "ck_background_job_outbox_status",
    "ck_background_job_outbox_attempts",
    "ck_background_job_outbox_timeout",
}.issubset(job_outbox_checks):
    raise SystemExit("Background-job outbox database check constraints missing")
outbox_columns = {item["name"] for item in inspector.get_columns("background_job_outbox")}
if not {
    "last_rq_job_id", "execution_attempts", "execution_generation",
    "execution_lease_token", "execution_lease_expires_at",
    "last_started_at", "completed_at", "last_execution_error_code",
}.issubset(outbox_columns):
    raise SystemExit("Durable background-job execution columns missing")
if not {
    "ck_background_job_outbox_execution_attempts",
    "ck_background_job_outbox_execution_generation",
}.issubset(job_outbox_checks):
    raise SystemExit("Background-job execution budget check constraints missing")
execution_unique = {
    item.get("name") for item in inspector.get_unique_constraints("background_job_execution_attempts")
}
if "uq_background_job_execution_generation_attempt" not in execution_unique:
    raise SystemExit("Execution attempt generation/number uniqueness missing")
execution_checks = {
    item.get("name") for item in inspector.get_check_constraints("background_job_execution_attempts")
}
if not {
    "ck_background_job_execution_attempt_number", "ck_background_job_execution_status",
}.issubset(execution_checks):
    raise SystemExit("Execution attempt status/budget checks missing")
execution_indexes = {
    item.get("name") for item in inspector.get_indexes("background_job_execution_attempts")
}
if not {
    "ix_background_job_execution_outbox_started",
    "ix_background_job_execution_status_started",
}.issubset(execution_indexes):
    raise SystemExit("Execution attempt history indexes missing")
replay_unique = {
    item.get("name") for item in inspector.get_unique_constraints("event_replay_baselines")
}
if "uq_event_replay_baseline_hypothesis" not in replay_unique:
    raise SystemExit("Frozen replay baseline uniqueness constraint missing")
replay_checks = {
    item.get("name") for item in inspector.get_check_constraints("event_replay_results")
}
if not {
    "ck_event_replay_result_counts", "ck_event_replay_result_score",
    "ck_event_replay_result_overlap", "ck_event_replay_result_comparison",
}.issubset(replay_checks):
    raise SystemExit("Event replay result check constraints missing")
for table, unique_name in (
    ("incident_response_plans", "uq_incident_response_plans_idempotency"),
    ("incident_action_dependencies", "uq_incident_action_dependencies_unique"),
):
    constraints = {item.get("name") for item in inspector.get_unique_constraints(table)}
    if unique_name not in constraints:
        raise SystemExit(f"Unique constraint missing: {table}.{unique_name}")
active_plan_index = {item.get("name") for item in inspector.get_indexes("incident_response_plans")}
if "uq_incident_response_plans_active_incident" not in active_plan_index:
    raise SystemExit("One-active-response-plan-per-incident index missing")
action_foreign_keys = inspector.get_foreign_keys("incident_actions")
if not any(
    item.get("referred_table") == "incident_response_plans"
    and item.get("constrained_columns") == ["response_plan_id"]
    for item in action_foreign_keys
):
    raise SystemExit("Response-plan foreign key from incident actions missing")
action_columns = {item["name"] for item in inspector.get_columns("incident_actions")}
if not {"response_plan_id", "sequence_order"}.issubset(action_columns):
    raise SystemExit("Response-plan action association columns missing")
action_indexes = {item.get("name") for item in inspector.get_indexes("incident_actions")}
if "uq_incident_actions_plan_sequence" not in action_indexes:
    raise SystemExit("Unique plan task sequence index missing")
discovery_columns = {item["name"] for item in inspector.get_columns("event_discovery_runs")}
if not {"trigger_kind", "dedupe_key"}.issubset(discovery_columns):
    raise SystemExit("Continuous event-discovery run metadata missing")
discovery_indexes = {item.get("name") for item in inspector.get_indexes("event_discovery_runs")}
if "uq_event_discovery_runs_dedupe_key" not in discovery_indexes:
    raise SystemExit("Continuous event-discovery deduplication index missing")
report_indexes = {item.get("name") for item in inspector.get_indexes("reports")}
if "ix_reports_updated_at" not in report_indexes:
    raise SystemExit("Continuous event-discovery scan index missing")
print("Continuous event-discovery deduplication contract: PASS")
print("Archive, candidate and response-plan idempotency constraints: PASS")
PY

printf '\n========== 6/9 · API ROUTE CONTRACTS =========='
printf '\n'
compose exec -T --interactive=false api python - <<'PY'
from app.api.main import app
# OpenAPI is FastAPI's stable route contract; app.routes can contain lazy routers.
http_methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
openapi_paths = app.openapi().get("paths", {})
routes = {
    (path, method.upper())
    for path, operations in openapi_paths.items()
    for method in operations
    if method.lower() in http_methods
}
required = {
    ("/api/integrations/backfills", "POST"),
    ("/api/integrations/backfills", "GET"),
    ("/api/integrations/backfills/{run_id}", "GET"),
    ("/api/integrations/backfills/{run_id}/resume", "POST"),
    ("/api/integrations/observations", "GET"),
    ("/api/integrations/observations/{observation_id}/replay", "POST"),
    ("/api/research/datasets/{dataset_id}/pair-candidates/mine", "POST"),
    ("/api/research/datasets/{dataset_id}/pair-candidates", "GET"),
    ("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}", "GET"),
    ("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}/resume", "POST"),
    ("/api/research/datasets/{dataset_id}/pair-candidates/{candidate_id}/dismiss", "POST"),
    ("/api/research/model-evaluations", "GET"),
    ("/api/intelligence/event-discoveries", "POST"),
    ("/api/intelligence/event-discoveries", "GET"),
    ("/api/intelligence/event-discoveries/{run_id}", "GET"),
    ("/api/incidents/{incident_id}/actions", "POST"),
    ("/api/incidents/{incident_id}/actions", "GET"),
    ("/api/incidents/{incident_id}/actions/{action_id}/events", "GET"),
    ("/api/incidents/{incident_id}/actions/{action_id}/approval", "POST"),
    ("/api/incidents/{incident_id}/actions/{action_id}", "PATCH"),
    ("/api/response-playbooks", "GET"),
    ("/api/incidents/{incident_id}/response-plans", "POST"),
    ("/api/incidents/{incident_id}/response-plans", "GET"),
    ("/api/incidents/{incident_id}/response-plans/{plan_id}", "GET"),
    ("/api/incidents/{incident_id}/response-plans/{plan_id}/cancel", "POST"),
    ("/api/admin/background-jobs/outbox", "GET"),
    ("/api/admin/background-jobs/outbox/{job_id}/retry", "POST"),
    ("/api/intelligence/event-replays", "POST"),
    ("/api/intelligence/event-replays", "GET"),
    ("/api/intelligence/event-replays/{run_id}", "GET"),
    ("/api/intelligence/event-replays/{run_id}/results", "GET"),
    ("/api/integrations/health", "GET"),
    ("/api/integrations/schedules", "GET"),
}
missing = sorted(required - routes)
if missing:
    raise SystemExit(f"Missing API routes: {missing}")
print(f"Acquisition, intelligence, operations, response-orchestration, outbox and replay routes: {len(required)}/{len(required)} present")
PY

printf '\n========== 7/9 · API, FRONTEND, DATABASE AND REDIS HEALTH =========='
printf '\n'
health=""
for attempt in $(seq 1 30); do
  if health="$(curl --silent --show-error --fail --max-time 4 http://127.0.0.1:8000/api/health 2>/dev/null)"; then break; fi
  sleep 2
done
[[ -n "$health" ]] || fail "API health endpoint failed."
printf '%s\n' "$health"
grep -Fq '"status":"ok"' <<<"$health" || fail "API unhealthy."
grep -Fq '"database":"ok"' <<<"$health" || fail "Database unhealthy."
grep -Fq '"redis":"ok"' <<<"$health" || fail "Redis unhealthy."
frontend="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:8080/ || true)"
printf 'Frontend HTTP: %s\n' "$frontend"
[[ "$frontend" == "200" ]] || fail "Frontend unhealthy."
compose exec -T --interactive=false db sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
compose exec -T --interactive=false redis sh -lc 'redis-cli ping 2>/dev/null | grep -qx PONG'
echo "API/frontend/PostgreSQL/Redis: PASS"

printf '\n========== 8/9 · PERSISTENT STORAGE =========='
printf '\n'
compose exec -T --interactive=false api sh -lc 'test -d /data/evidence && test -w /data/evidence && test -d /data/models && test -w /data/models && echo "API evidence/model storage writable: PASS"'
compose exec -T --interactive=false worker sh -lc 'test -d /data/models && test -r /data/models && echo "Primary worker model storage readable: PASS"'
compose exec -T --interactive=false worker-bulk sh -lc 'test -d /data/models && test -r /data/models && echo "Bulk worker model storage readable: PASS"'

printf '\n========== 9/9 · LIVE ACQUISITION SAFETY =========='
printf '\n'
echo "No provider smoke test or live ingestion was triggered by this verifier."
echo "Run scripts/smoke_external_sources.py for read-only network checks."
echo "Run scripts/verify_live_acquisition_e2e.py only when ready to write bounded real USGS data."
printf '\nALL CIGNAL ACQUISITION, INTELLIGENCE, INCIDENT OPERATIONS, RESPONSE ORCHESTRATION, EVENT REPLAY AND DISTRIBUTED JOB RELIABILITY QUALITY GATES PASSED.\n'
printf 'Migration: 0021_job_execution_reliability\n'
printf 'No existing database/evidence/Redis/model volumes were reset.\n'
