# Ingestion Observability and Source-Time Provenance

## Purpose

Cignal records an immutable operational result for every configured connector attempt. This makes source reachability and record-processing outcomes observable without confusing a configured connector with a live, healthy source.

## Per-source run record

`integration_runs` stores:

- source type and display name;
- trigger (`scheduled`, `manual`, or `system`);
- status (`running`, `succeeded`, `partial`, `failed`, or `empty`);
- fetched, inserted, duplicate, rejected, queued, and queue-failure counts;
- categorized record rejection counts;
- exception class only, retryability, start/completion timestamps and duration.

Raw exception messages, request URLs, authorization headers, response bodies and provider credentials are deliberately not persisted. Worker logs may contain diagnostic tracebacks; they must be protected as operational logs.

A `partial` run means valid records were preserved while one or more records were rejected, or AI queueing failed for an inserted record. A `failed` run means connector execution or an unexpected persistence operation failed. An `empty` run is a successful fetch that returned no records.

## Record handling

Each record is validated independently. A source-type mismatch, empty title, unpaired coordinates or coordinates outside geographic bounds is rejected with a reason counter. The remaining valid records from the same feed continue through ingestion.

Provider source IDs are normalized to the database limit with a digest suffix for long IDs. Adapters preserve the original ID until this central normalization step. Existing records are treated as duplicates. A uniqueness conflict from a concurrent ingestion worker is also counted as a duplicate. Newly inserted reports and previously submitted-but-unanalysed reports are queued through deterministic RQ job IDs. An already queued/running analysis is reused rather than duplicated. A bounded 100-report backlog sweep also recovers citizen or historical submissions whose initial queue operation failed.

Source URLs are validated as HTTP(S), stripped of URL userinfo and credential-bearing query parameters, and discarded when malformed or oversized. This prevents configured API keys and bearer-style query tokens from leaking into persisted provenance or report responses.

## Event time vs ingestion time

`reports.created_at` remains the time Cignal stored the report. `reports.source_observed_at` records the provider's event/observation time when available. Correlation uses `source_observed_at` first and falls back to `created_at` for citizen submissions and sources without timestamps.

Connector timestamp parsing accepts ISO-8601 strings, epoch seconds/milliseconds and RSS parsed-time tuples. Invalid optional timestamps are left null; they do not cause otherwise valid source records to be discarded.

## API

- `GET /api/integrations/health` — configured sources enriched with recent persisted outcome state; admin/analyst only.
- `GET /api/integrations/runs` — bounded recent run history with optional `source_type` and `status_filter`; admin/analyst only.
- `POST /api/integrations/ingest` — queue a manual run; admin/analyst only.

The existing `GET /api/integrations` remains the connector configuration catalogue. `configured=true` means only that the adapter has the required configuration; the health endpoint reports observed execution outcomes.

## Status interpretation

- `needs_configuration`: connector requires operator configuration/credentials.
- `not_tested`: configured but no persisted run exists.
- `running`: a connector run is currently active and started less than two hours ago.
- `healthy`: latest run completed without record/queue failures, including a valid empty fetch.
- `degraded`: latest run completed with rejected records or queue failures.
- `error`: latest run failed.
- `stale`: latest successful run completed more than 48 hours ago, or a run has remained marked `running` for more than two hours.

A future scheduled run may recover a provider after the bounded per-fetch retry policy is exhausted. Transient network/HTTP failures are retried immediately with capped exponential backoff; this is not an unbounded retry loop. Scheduled and manual ingestion triggers share one deterministic RQ job ID, so one source-ingestion job runs at a time; completed/failed jobs are replaced on the next trigger. The scheduler lease is held for the interval to avoid duplicate scheduling across scheduler replicas.


## Analysis generations and retry telemetry (migration 0009)

Reports carry `analysis_revision` and `analyzed_revision`. A report starts at revision 1; every committed follow-up or evidence upload advances the revision with an atomic database-side increment, so concurrent uploads cannot collapse into the same generation. A partial index supports the pending-generation sweep. Analysis jobs are keyed by report ID plus revision (`cignal-report-analysis-{report_id}-r{revision}`), so a completed job for an older revision cannot suppress reanalysis. Workers compare the requested revision both before computation and under a row lock before committing. If the report changes during analysis, the stale result is discarded and the newest revision is queued. The ingestion backlog sweep also queues every report where `analysis_revision > analyzed_revision`, not only reports that have never received an AI category.

Connector fetches use bounded exponential backoff with jitter for transient network failures, HTTP 408/425/429 and HTTP 5xx responses. `Retry-After` is honored up to the configured maximum. Permanent client errors and malformed payloads are not retried. `IntegrationRun.fetch_attempts` records the number of source fetch attempts. Defaults are three attempts, a 0.5-second base delay and an 8-second maximum delay; the maximum attempt count is capped at five.

`records_inserted` counts only rows committed to the database. If a connector fails after a partial uncommitted batch, that batch is rolled back and is excluded from the run's inserted count.
