# Acquisition & Intelligence Research Engine v1

This release combines two substantial workstreams over the existing acquisition-operations (`0014`) and research-governance (`0012`) foundations.

## 1. Durable historical acquisition

### Providers

- **USGS FDSN Event Web Service**: bounded GeoJSON historical windows using `starttime`, `endtime`, `orderby=time-asc`, `limit`, and `offset`.
- **NASA EONET v3**: bounded inclusive date windows (maximum 14 calendar dates per request). EONET does not expose the same offset pagination contract, so Cignal bounds each response, applies a deterministic local offset, and refuses an ambiguous full-sized response rather than silently skipping potentially truncated events.

Historical access is intentionally separated from frequent live polling. The USGS realtime feeds remain the preferred low-latency path; FDSN is used for historical queries. EONET is a curated event catalogue, not an impact/severity authority. Provider categories, geometry and magnitude metadata are retained as provider metadata and are not interpreted as casualty or damage estimates.

### Immutable observation archive

`acquisition_observations` stores a bounded normalized payload snapshot, a SHA-256 content digest, source record identity, first/last-seen timestamps, seen count and linked report. Repeated identical records increment `seen_count`; changed provider content creates another immutable observation version. The retrieval timestamp and live-vs-historical acquisition mode are excluded from the content digest because they describe Cignal's acquisition path, not provider content.

`acquisition_backfill_runs` stores the requested time range, provider cursor/offset, record caps, counters, lease and outcome. `acquisition_backfill_items` binds each run to the observations/reports it captured and records analysis-queue state. This per-run ledger means a resume only retries analysis work belonging to that run; it does not sweep or enqueue unrelated historical reports.

The report's provider identity remains unique across live and historical ingestion. For EONET, a revision-suffixed live key is resolved to the stable EONET event identifier before report persistence; payload revisions remain distinguishable in the observation archive.

### Operational safeguards

- One active lease per backfill run; expired leases can be recovered by the scheduler.
- Committed page/cursor state makes a stopped run resumable.
- Deterministic RQ job IDs reduce duplicate queue dispatch after scheduler/worker restarts.
- Source/response bounds and per-run maximum record caps prevent unbounded fetches.
- Provider and queue errors are recorded separately.
- Replaying an archived observation only requeues analysis for its linked report; it does not rewrite evidence or report source content.
- Installation and verification do not trigger live ingestion.

## 2. Event-pair active learning

`pair_candidate_mining_runs` records each bounded candidate-mining attempt. `event_pair_candidates` stores report pairs, pair fingerprint, heuristic and optional supervised scores, candidate-selection score, model version and reason metadata.

Candidate mining prioritizes uncertain matches and hard-negative hints (for example, textually similar reports with materially conflicting geography or timing). It does **not** assign `same_event` or `different_event` labels. Candidates remain `pending` until an authorized analyst reviews and labels them using the existing research-label workflow. Existing exact-pair fingerprints prevent repeated candidate generation from multiplying review work.

The process is bounded by maximum reports, time window, geographic distance, scored pairs and output candidates. It filters to the active dataset's permitted source types. Candidate scoring is a triage/ranking aid, not event confirmation.

## 3. Reproducible model evaluation

The external event-pair evaluator now emits classification and ranking metrics (`precision@k`, `recall@k`) alongside the heuristic baseline comparison. It checks model artifact schema and rejects an external evaluation set whose event groups overlap training groups. `--persist` stores the dataset SHA-256, model artifact SHA-256, sample/group counts, metrics, comparison and evaluation policy in `model_evaluation_runs`.

Metrics are descriptive of the supplied labelled holdout only. They do not prove statistical significance, probability calibration, geographic generalization or operational safety. Report the benchmark dataset's collection period, geography, source composition and annotation policy alongside any results.

## 4. New database tables (migration `0015_acq_archive_learning`)

- `acquisition_observations`
- `acquisition_backfill_runs`
- `acquisition_backfill_items`
- `pair_candidate_mining_runs`
- `event_pair_candidates`
- `model_evaluation_runs`

The migration is additive and has an idempotent table/index creation strategy. Do not reset PostgreSQL, Redis, evidence or model volumes.

## 5. APIs

Historical backfills (admin-only creation/resume):

- `POST /api/integrations/backfills`
- `GET /api/integrations/backfills`
- `GET /api/integrations/backfills/{run_id}`
- `POST /api/integrations/backfills/{run_id}/resume`

Observation archive:

- `GET /api/integrations/observations`
- `POST /api/integrations/observations/{observation_id}/replay`

Analyst/admin candidate mining and evaluation history:

- `POST /api/research/datasets/{dataset_id}/pair-candidates/mine`
- `GET /api/research/datasets/{dataset_id}/pair-candidates`
- `GET /api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}`
- `POST /api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}/resume`
- `POST /api/research/datasets/{dataset_id}/pair-candidates/{candidate_id}/dismiss`
- `GET /api/research/model-evaluations`

Human labels continue to be created and reviewed through the existing event-pair label API; the candidate miner never labels automatically.

## 6. Operator scripts

Run commands inside the API container from the Cignal project root:

### Read-only provider smoke test

```bash
docker compose exec -T --interactive=false api python scripts/smoke_external_sources.py
```

This performs bounded GET requests only. A provider disabled by configuration is reported as skipped; it does not write Cignal database records.

### Bounded historical backfill

```bash
docker compose exec -T --interactive=false api python scripts/run_historical_backfill.py \
  --source usgs --start 2026-11-03T00:00:00Z --end 2026-11-03T23:59:59Z \
  --window-days 1 --max-records 500
```

The command creates a durable run and executes it synchronously. Use short date ranges first. `--source nasa_eonet` is also supported when that connector is enabled; each request is at most 14 inclusive dates. A result of `limit_reached` means the requested record cap was reached and the run needs an explicit resume with a higher cap.

### Live end-to-end / repeat-ingestion check

```bash
docker compose exec -T --interactive=false api python scripts/verify_live_acquisition_e2e.py
```

This is **write-enabled**: it backfills the previous completed UTC day twice (up to 500 records per run), then checks the repeat run inserted no new reports. It is bounded but creates report, observation, run and analysis-queue records. Run only when ready to populate the database with real USGS historical data.

### Candidate mining

```bash
docker compose exec -T --interactive=false api python scripts/mine_event_pair_candidates.py \
  --dataset-slug YOUR_ACTIVE_DATASET_SLUG --max-reports 1200 --top-candidates 500
```

This creates pending review candidates only; it does not create labels. Replace the slug with an existing active dataset.

### Persist an external holdout evaluation

```bash
docker compose exec -T --interactive=false api python scripts/evaluate_event_pair_model.py \
  /data/research/heldout-event-pairs.jsonl \
  --model /data/models/event-pair-model.joblib --persist
```

The JSONL and model paths are examples; use actual files mounted in the API container. Evaluation refuses training/holdout group overlap.

## 7. Verification boundaries

The installation quality gate runs compile, the full pytest suite, Ruff and high-severity Bandit, verifies the migration and schema, checks API/front-end/Redis/PostgreSQL health, confirms the expected routes and storage access, and does not trigger ingestion.

The separate provider smoke and end-to-end scripts are not part of the install gate because they depend on external network availability and/or intentionally write live data. Run them explicitly and retain their JSON output with the corresponding acquisition-run IDs.
