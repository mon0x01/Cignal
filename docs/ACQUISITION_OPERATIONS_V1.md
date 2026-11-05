# Acquisition Operations Engine v1

## Purpose

The Data Acquisition Engine v1 added provider connectors and source-governance metadata. This layer makes those connectors operationally independent. Each configured provider now has a durable polling cadence, a recoverable lease, and its own retry history. The existing report ingestion path remains responsible for normalization, validation, provenance, idempotent persistence, and analysis queueing.

## Runtime flow

1. The scheduler enumerates configured connectors.
2. PostgreSQL stores one `acquisition_source_states` row per `source_type`.
3. Newly seen sources are initialized for their first poll interval; enabling a connector does not cause an immediate burst of requests.
4. When a source is due, the scheduler claims a UUID lease with an expiry and enqueues a deterministic RQ job for that source only.
5. The worker validates the lease token before fetching. A superseded or expired queued job is skipped.
6. Connector retries remain bounded by the existing fetch retry policy. Each source outcome is written to `integration_runs` and summarized in `acquisition_source_states`.
7. A transient failure schedules capped exponential backoff. A successful/empty/partial run clears the failure streak and returns to the normal poll interval.
8. A worker crash leaves the lease to expire. The scheduler can then claim the source again. Replayed feed records are protected by the existing unique `(source_type, source_record_id)` constraint.

## State and concurrency semantics

`acquisition_source_states` is the durable authority for next-run time and worker lease. Redis/RQ is transport, not the sole record of whether work is due. A lease lasts longer than the configured RQ job timeout. A scheduler tick lock prevents multiple scheduler instances from claiming the same due set simultaneously; PostgreSQL row locks protect individual claims. The worker checks the lease token so an old queued job cannot start after a newer claim was issued.

Manual global ingestion remains available. A manual run does not overwrite an active scheduled lease. Source-scoped manual ingestion is available at `POST /api/integrations/ingest/{source_type}`. Scheduling state is visible at `GET /api/integrations/schedules`; both endpoints require the existing admin/analyst role.

The unique source-record constraint remains the final persistence boundary. If two paths fetch the same upstream item concurrently, only one report is stored; the other is recorded as a duplicate. Reprocessing is safe, but the source's raw response is not archived by this layer.

## Cadence and retry configuration

- `INGEST_INTERVAL_MINUTES=30` is the fallback cadence for every configured source (minimum effective default: five minutes).
- `INGEST_SOURCE_INTERVAL_OVERRIDES=` accepts semicolon-separated `source_type=seconds` entries; each override must be between 60 and 86400 seconds. Example: `usgs=900;nasa_eonet=1800;sachet_cap=900`.
- `INGEST_SCHEDULER_TICK_SECONDS=30` controls how often the scheduler checks for due sources (bounded to 15–300 seconds).
- `INGEST_JOB_TIMEOUT_SECONDS=1200` is the RQ worker timeout; `INGEST_JOB_LEASE_SECONDS=1800` is the requested lease (the runtime always extends it beyond the job timeout).
- `INGEST_RETRY_BACKOFF_BASE_SECONDS=60` and `INGEST_RETRY_BACKOFF_MAX_SECONDS=21600` control retryable-failure backoff.

No provider-specific rate-limit is inferred. Operators must choose poll intervals that comply with the selected provider's terms and published limits. The default 30-minute interval is conservative but is not a promise that a provider supports that exact cadence.

## Operational limitations

- A configured connector does not imply that credentials, endpoints, subscription identifiers, or upstream reachability have been validated.
- A scheduled source run is an observation-ingestion operation, not confirmation of an incident. Human review remains required.
- Replays currently mean re-fetching the upstream source and relying on idempotent source identifiers; a durable raw-payload archive and provider cursor/watermark replay are future work.
- The scheduler records source-level health and retry state but does not claim delivery guarantees stronger than the upstream provider and RQ/PostgreSQL can provide.
- The verification script does not initiate a live poll.

## Migration

`0014_acq_source_sched` adds `acquisition_source_states`. It does not alter report/evidence rows or existing integration-run history. The installer requires Alembic to be exactly at `0013_source_provenance`, creates a source backup and PostgreSQL custom-format backup, applies migration 0014 once, and rebuilds only `api`, `frontend`, `worker`, and `scheduler`.
