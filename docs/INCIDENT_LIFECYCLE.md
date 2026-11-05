# Incident review and lifecycle

## Purpose

Cignal keeps three separate records: an incoming `Report`, its `Evidence`, and a real-world `Incident`. AI outputs are triage hints. Only an authenticated administrator or analyst can make a review decision that creates or links an incident.

## Report review decisions

| Decision | Preconditions | Result |
|---|---|---|
| `verify` | Report is `submitted`, `analyzed`, or `needs_info` | Creates a new `verified` incident and links the report. |
| `verify` with `incident_id` | Target incident is `verified` or `monitoring` | Links report to the selected existing incident. |
| `link` | An `incident_id` is supplied; target is `verified` or `monitoring` | Adds a supporting report relationship. A repeated link to the same incident returns 409. |
| `request_info` | Report belongs to an active registered citizen | Sets `needs_info` and creates an in-app notification for the submitter. |
| `reject` | Report is not already rejected or verified | Sets terminal status `rejected` and notifies a registered submitter. |

A rejected report cannot later be verified or linked. A verified report can be linked to another open incident, but cannot be re-verified or rejected. An incident in `resolved` or `closed` state cannot accept new supporting reports.

The API locks the report row during review and the target incident row during linking on databases that support `SELECT FOR UPDATE` (PostgreSQL). Association uniqueness is enforced by the composite primary key on `report_incidents`. `source_count` is recomputed from the association table after a link is inserted.

## Incident lifecycle

Allowed status transitions are forward-only:

```text
verified -> monitoring -> resolved -> closed
    |             |             |
    +------------>+------------> closed
    +--------------------------> resolved
```

More explicitly: `verified` may move to `monitoring`, `resolved`, or `closed`; `monitoring` may move to `resolved` or `closed`; `resolved` may move to `closed`; `closed` is terminal. A closed incident cannot be edited.

## Responder visibility

Responders can list incidents and read incident detail, including linked report summaries and provenance. They cannot browse the general report inbox, make analyst decisions, change incident status, or download unreviewed evidence. In-app notifications are created when a report is linked to an incident.

## API surface

- `POST /api/reports/{report_id}/review` — analyst decision (`verify`, `reject`, `request_info`, `link`).
- `GET /api/incidents` — incident list for admin, analyst, responder.
- `GET /api/incidents/{incident_id}` — incident detail plus backward-compatible `reports` and enriched `report_links` (`relationship_type`, `linked_at`, report, `evidence_count`).
- `PATCH /api/incidents/{incident_id}` — forward lifecycle transition and/or severity/summary update for admin/analyst.

## Validation

The workflow test exercises administrator MFA, report submission, analyst verification, citizen request/follow-up, linking a second report, duplicate-link rejection, responder notifications and visibility, report-inbox denial for responders, and forward-only incident transitions. Run in the project environment:

```bash
python -m pytest -q
python -m ruff check app tests
```

The test suite uses a disposable SQLite database; PostgreSQL-specific row locking is exercised only in the deployed PostgreSQL stack. Do not treat successful local tests as a substitute for a PostgreSQL concurrency test or operational review.


## Operational activity timeline

Operational staff can append activity records to incidents. Supported activity types are observations, situation updates, resource requests, access constraints, shift handovers and general notes. The timeline snapshots the author's display name and role so the attribution remains understandable if an account is later deactivated or removed. Entries are audited and broadcast to operational WebSocket clients.

Responders can create activity records but cannot alter incident severity or lifecycle state. Closed incidents reject new activity and are read-only. Timeline entries do not represent dispatch orders, resource allocation approvals, or official emergency declarations.


## Operational actions

Durable work items are separate from activity notes. Each action has an owner, priority, due date, state, version and append-only history. Public warnings, evacuation, resource dispatch and medical intervention are proposal-only until explicitly approved by an administrator or analyst. The API does not execute external dispatch or alerting. See `docs/INCIDENT_OPERATIONS_V1.md` for state transitions and endpoints.
