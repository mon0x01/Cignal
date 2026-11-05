# Incident Operations Control Plane v1

## Purpose

The Operations Control Plane turns a verified incident into a durable, accountable work queue. It complements (rather than replaces) the incident activity timeline:

- **Activity** is a chronological field note, observation, handover or resource request.
- **Action** is a trackable unit of operational work with an owner, priority, due time, state, approval gate and audit history.

Actions are attached only to verified incidents. Resolved and closed incidents reject new actions.

## Safety boundary

Cignal treats these action types as approval-gated:

- `public_warning`
- `evacuation`
- `resource_dispatch`
- `medical_intervention`

Creation only records a **proposed** action. It never sends a warning, dispatches a responder, allocates a resource or instructs a medical intervention. An administrator or analyst must explicitly approve or reject the proposal through the approval endpoint. Only an approved action can move to `in_progress`.

The approval is a recorded application-level authorization, not proof that a real-world authority has jurisdiction. Integrations that actually transmit public alerts or dispatch requests are intentionally outside this release.

## State model

```text
proposed -> approved -> in_progress -> completed
    |                     |   |
    +-> cancelled         +-> blocked -> in_progress
    |                     +-> cancelled
    +-> rejected (approval decision)
```

For non-gated action types, an operator may approve the proposal through the normal status endpoint. Gated actions must use the dedicated approval endpoint. Completed, rejected and cancelled actions are terminal.

Every update requires `expected_version`. A stale client receives HTTP 409 with the current version instead of silently overwriting another operator's change. Every creation, approval decision and state transition appends an `IncidentActionEvent`; events are not editable through the API. All mutations also write the existing audit log and publish an incident event.

## API

All routes require an authenticated admin, analyst or responder unless stated otherwise.

- `POST /api/incidents/{incident_id}/actions` — create a proposal.
- `GET /api/incidents/{incident_id}/actions` — list actions, optionally filter by status.
- `GET /api/incidents/{incident_id}/actions/{action_id}/events` — read action history.
- `POST /api/incidents/{incident_id}/actions/{action_id}/approval` — admin/analyst approval or rejection.
- `PATCH /api/incidents/{incident_id}/actions/{action_id}` — version-checked status transition.

Example proposal:

```json
{
  "title": "Issue evacuation warning",
  "description": "Prepare a geographically scoped evacuation warning for review.",
  "action_type": "evacuation",
  "priority": "urgent"
}
```

Example approval:

```json
{
  "decision": "approve",
  "note": "Reviewed scope and supporting incident evidence."
}
```

Example status update:

```json
{
  "status": "in_progress",
  "expected_version": 2,
  "note": "Authorized execution started."
}
```

## Data model

Migration `0016_incident_operations` adds:

- `incident_actions`: current task state, assignment, priority, due date, approval metadata and optimistic-lock version.
- `incident_action_events`: append-only creation, approval and transition history.

The migration is additive. Do not reset PostgreSQL, Redis, evidence or model volumes.

## Known boundaries

- No external dispatch, SMS, email alerting or resource inventory mutation is performed by an action.
- The current approval role gate is application-level; production deployment still needs an organization-specific authorization policy and legal authority mapping.
- Event history is append-only through the API, but database administrators can still alter database contents.
- Task assignment is not a claim that the assigned responder has acknowledged or accepted the task. Acknowledgement/shift and resource-capacity workflows are later operational capabilities.
- A completed action records operator assertion; it is not independently verified by a field sensor or external system.


## Response playbooks and dependencies

Cignal can create a versioned coordination plan for a verified flood, earthquake, fire, cyclone or landslide incident. Generated tasks retain the existing action approval and transition rules, and dependency edges prevent a task from entering `in_progress` until its prerequisites are complete. See `docs/INCIDENT_RESPONSE_ORCHESTRATION_V1.md`. These templates are not official emergency procedures and have no external execution side effects.
