# Continuous Cross-Source Event Discovery v1

Migration: `0019_continuous_event_discovery` (head after this update).

## Purpose

Cignal previously exposed event-hypothesis discovery as an analyst-triggered job.
This update adds a conservative scheduler path so newly analyzed, located reports
from independent source origins can produce reviewable event candidates without
requiring an analyst to remember to start each discovery run.

## Trigger policy

The scheduler considers reports that:

- have completed the current analysis revision;
- are not rejected;
- have valid latitude/longitude values;
- are one of flood, earthquake, fire, cyclone, or landslide;
- fall inside the configured event-observation window; and
- collectively represent at least two distinct `(source_type, source_name)` origins.

It waits for a configurable quiet period after the newest report update. It then
creates one discovery run for that report generation. A SHA-256 generation key,
backed by a unique database index, prevents multiple scheduler instances from
creating duplicate runs for the same settled generation.

## Durable delivery

The `event_discovery_runs` row and `background_job_outbox` request are created in
the same database transaction. The scheduler's existing outbox relay later publishes
the job to RQ using the deterministic ID `cignal-event-discovery-{run_id}`. Redis/RQ
outage therefore delays delivery but does not lose the discovery request.

Analyst-triggered discovery uses the same outbox path. The API returns HTTP 202 with
`dispatch_status=pending`; it no longer reports a queue outage as a failed request
when the durable database write succeeded.

## Configuration

- `CONTINUOUS_EVENT_DISCOVERY_ENABLED=true`
- `CONTINUOUS_EVENT_DISCOVERY_QUIET_SECONDS=120`
- `CONTINUOUS_EVENT_DISCOVERY_WINDOW_HOURS=72`
- `CONTINUOUS_EVENT_DISCOVERY_MAX_REPORTS=300`

Quiet seconds are clamped to 30–3600, window hours to 1–168, and report cap to 2–500.

## Safety boundaries

Discovery creates and updates `EventHypothesis` candidate records only. It does not
create incidents, verify citizen reports, approve life-safety operations, send public
warnings, dispatch resources, or initiate response plans. Existing complete-link
clustering, scoring explanations, semantic/model score caveats, and analyst review
controls remain authoritative.

## Operational observability

Each run records `trigger_kind` (`analyst` or `continuous`) and the dedupe key when
scheduled automatically. Existing event-discovery list/detail APIs expose the
trigger kind. Outbox status remains visible via the existing admin background-job
outbox endpoint.
