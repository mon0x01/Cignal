# Incident Response Playbooks & Dependency Orchestration v1

## Purpose

Response playbooks provide a versioned coordination checklist for an already verified incident. They turn a static collection of incident actions into a bounded, inspectable plan with explicit prerequisites and progress.

A playbook is workflow scaffolding, **not an official emergency SOP, tactical instruction, or authority to act**. Cignal does not issue public warnings, make evacuation decisions, dispatch responders or resources, allocate medical services, or call external systems. Tasks that use `public_warning`, `evacuation`, `resource_dispatch`, or `medical_intervention` remain behind the existing administrator/analyst approval gate. Approval changes the Cignal task state only; it does not perform an external side effect.

## Core records

Migration `0017_response_plan_orchestration` adds:

- `incident_response_plans`: incident, template key/version, immutable template snapshot, lifecycle state and per-incident idempotency key.
- `incident_actions.response_plan_id` and `sequence_order`: associates generated actions with their plan while preserving all existing actions (the new column is nullable).
- `incident_action_dependencies`: immutable prerequisite edges between tasks in a response plan.

Only one plan may be `active` for an incident at a time. Completed, cancelled and `needs_review` plans remain as history. Creating the same request with the same `Idempotency-Key` returns the original plan rather than creating duplicate tasks. Reusing a key with different content is rejected.

## Dependency semantics

A task may be approved before its prerequisites complete, but it cannot move to `in_progress` until every prerequisite task is `completed`. The API returns the blocking task IDs, titles and states with HTTP 409 when this rule is violated. A task that needs an explicit life-safety approval must also pass the existing dedicated approval endpoint.

The bundled templates are deliberately conservative coordination checklists: establish a shared situation picture; review evidence coverage; record reported impact locations and access constraints; coordinate documented resource requests; and prepare public information for authorized review. No due dates or automatic resource allocation are invented.

The plan becomes `completed` only when all its tasks are completed. Rejection or cancellation of a plan task moves the plan to `needs_review`. An administrator or analyst may cancel an active or needs-review plan if no task remains `in_progress`. Cancelling a plan does not silently rewrite task history; it prevents new plan tasks from starting. An incident cannot transition to `resolved` or `closed` while an active or needs-review plan remains.

## API

- `GET /api/response-playbooks?disaster_type=flood` — read the versioned catalogue (admin, analyst, responder).
- `POST /api/incidents/{incident_id}/response-plans` — create a matching plan (admin, analyst); requires `Idempotency-Key` and `{ "template_key": "flood_coordination_v1" }`.
- `GET /api/incidents/{incident_id}/response-plans` — list plan summaries.
- `GET /api/incidents/{incident_id}/response-plans/{plan_id}` — inspect the template snapshot, tasks, dependencies, blockers and progress.
- `POST /api/incidents/{incident_id}/response-plans/{plan_id}/cancel` — cancel a plan (admin, analyst), subject to the in-progress-task guard.

Existing action APIs remain the only way to approve and transition actions. Manual actions not associated with a response plan retain their previous behavior.

## Integrity and operational boundaries

- Plan creation, generated actions, task events, dependency edges and audit entry are committed in one database transaction.
- Database uniqueness enforces idempotency and one active plan per incident; task sequence and dependency edges have unique constraints.
- The playbook definition is snapshotted on plan creation so later template edits cannot rewrite historical plans.
- Prerequisite checks run server-side when a task is moved to `in_progress`; UI state is never trusted as an authorization boundary.
- Plan generation is explicit and analyst/admin initiated. It does not occur from an unverified event hypothesis or AI score.
- No background job or external provider call is triggered by this feature.

## Verification

The automated suite covers template graph validation, plan-state progression, schema migration idempotency, API idempotency, prerequisite enforcement, approval-gated task behavior, plan cancellation and incident lifecycle protection. Production PostgreSQL concurrency still needs a deployed integration test; SQLite tests do not prove PostgreSQL row-lock behavior.
