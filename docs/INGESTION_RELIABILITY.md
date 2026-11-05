# Cignal — Analysis and Ingestion Reliability v1

**Database migration:** `0009_analysis_retry_v1`
**API version:** remains `0.1.0`
**Purpose:** close correctness gaps in report reanalysis and connector execution without changing the Incident/Report/Evidence domain model or removing existing data.

## Report analysis generations

`analysis_revision` records the latest committed report/evidence generation. `analyzed_revision` records the latest generation whose analysis was committed. New reports start at revision 1 and analyzed revision 0. A follow-up or evidence upload advances the revision atomically in SQL, so simultaneous uploads cannot overwrite each other's generation number.

RQ analysis job IDs contain both report ID and revision. A completed job for revision 1 therefore cannot block revision 2. Workers reject obsolete revisions before analysis and check the generation again under a row lock before committing. If a report changes while an analysis is running, the old result is discarded and the latest generation is queued. The scheduled ingestion backlog sweep recovers every report where the requested revision exceeds the analyzed revision. It does not treat an AI category alone as proof that the latest report content was analyzed.

This mechanism provides stale-result protection and at-least-once recovery, not exactly-once execution. Two workers may perform duplicate computation in a narrow race, but only one can commit a given generation as current.

## Connector retry policy

Connector `fetch()` calls receive a bounded retry policy. It retries likely-transient transport/timeouts, HTTP 408, 425, 429 and HTTP 5xx responses. Other 4xx errors and malformed payloads are not retried. Delay uses exponential backoff plus jitter and honors numeric or HTTP-date `Retry-After` values up to the configured cap.

Settings and defaults:

| Setting | Default | Bound |
|---|---:|---:|
| `INTEGRATION_RETRY_ATTEMPTS` | 3 | 1–5 |
| `INTEGRATION_RETRY_BASE_DELAY_SECONDS` | 0.5 | 0–30 seconds |
| `INTEGRATION_RETRY_MAX_DELAY_SECONDS` | 8 | 0–30 seconds |

`IntegrationRun.fetch_attempts` records attempts on both success and exhausted failure. A later scheduled/manual cycle can recover a failed source; the current worker does not create an unbounded background retry loop.

## Transaction accounting

`records_inserted` represents committed report rows only. Records are committed in bounded batches of 100. If a later batch fails, earlier committed rows remain accurately counted, while the currently uncommitted batch is rolled back and excluded. SQLite test runs explicitly begin the transaction before per-record savepoints to match PostgreSQL rollback semantics.

## Validation performed on the source snapshot

- Python compilation: passed.
- Pytest: 157 passed.
- `bash -n scripts/verify.sh`: passed.
- `node --check app/ui/static/service-worker.js`: passed.
- Full Ruff and Bandit scans were not available in the isolated build environment; the updated strict verifier runs both inside the project's Docker API image.

This release has not been tested against the user's post-reboot Docker/PostgreSQL/Redis environment yet. The installer must confirm the existing revision is 0008, create a pre-migration database backup, apply migration 0009, rebuild the application services and run the complete quality gate.
