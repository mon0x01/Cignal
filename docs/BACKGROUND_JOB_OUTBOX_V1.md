# Durable Background-Job Outbox v1

Migration `0018_durable_job_outbox` introduces a transactional outbox for
report-analysis jobs. Migration `0019_continuous_event_discovery` extends the
same delivery path to analyst-triggered and continuous event-hypothesis discovery.
A source mutation or discovery-run request commits together with its durable job
intent, so Redis/RQ availability affects dispatch latency, not request persistence.

## Delivery model

1. API/worker code calls `enqueue_report_analysis(db, report_id, revision)`
   before committing the source transaction. Event-discovery requests call
   `enqueue_event_discovery(db, run_id, parameters)` in the run-creation transaction.
2. The scheduler claims due rows using `FOR UPDATE SKIP LOCKED` where supported,
   assigns a lease token/expiry and commits the claim.
3. It enqueues the task in RQ using a deterministic job ID derived from report
   ID and analysis revision.
4. Successful delivery marks the outbox row `enqueued`. If the process crashes
   after RQ accepted the task but before the database update, the next lease
   owner checks the same RQ job ID and does not create duplicate work.
5. Delivery errors use bounded exponential backoff. After 12 unsuccessful
   dispatch attempts, the row becomes `dead` and remains inspectable.
6. Administrators can inspect bounded outbox metadata and explicitly retry a
   dead-lettered row through the admin API.

## Guarantees and boundaries

- Source mutation and job request persistence are atomic in PostgreSQL.
- Delivery is at-least-once; deterministic RQ IDs make dispatch retries
  idempotent. The task itself must still be idempotent.
- `enqueued` means RQ accepted or already held the deterministic job. It does
  not mean report analysis completed successfully.
- Worker execution failures remain distinct from outbox delivery failures.
  Existing analysis-generation guards and pending-report recovery remain in
  place for execution recovery.
- Durable dispatch supports `app.worker.process_report` and
  `app.worker.discover_event_hypotheses`. Other RQ workflows continue using their
  existing queueing and lease logic.
- Continuous event discovery is coalesced by a quiet period and a unique
  generation key; see `CONTINUOUS_EVENT_DISCOVERY_V1.md`.
- The outbox never makes an automated emergency decision or triggers an
  external warning/resource dispatch.

## Operations

- `GET /api/admin/background-jobs/outbox` reports counts and recent metadata.
- `POST /api/admin/background-jobs/outbox/{job_id}/retry` releases a dead row.
- No job arguments or report text are exposed by the status endpoint.
- Rows are retained for audit; retention/archival policy will be defined only
  after production volume and operational requirements are measured.
