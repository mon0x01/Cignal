# Incident operational activity timeline

## Purpose

The timeline gives operational teams a shared, attributable record of observations and coordination notes attached to a verified incident. It does not replace radio/dispatch procedures, official logs, or command authority.

## Activity types

- `observation`: a field observation with source/time context where known.
- `situation_update`: a concise description of changed conditions.
- `resource_request`: a request for support; it is not a dispatch or approval.
- `access_constraint`: a road, route, facility or safety access limitation.
- `handover`: information passed between shifts or teams.
- `note`: a general operational note.

## Permissions and state rules

Admin, analyst and responder roles can read and append timeline entries. Citizens cannot read or write incident timelines. Responder access is intentionally limited to operational notes: responders cannot change incident severity, summary or status. Admins and analysts retain the existing incident lifecycle permissions. Closed incidents reject new activity and their timeline is read-only. Resolved incidents may still receive notes for post-resolution documentation until formally closed.

## Data model

Each entry stores a UUID, incident foreign key, nullable actor foreign key, actor display-name and role snapshots, activity type, message, optional location label/coordinate pair, and creation timestamp. Activity rows are append-only through the exposed application API: no update/delete route is provided. The database migration `0004_incident_activity_timeline` adds one table and indexes. It does not rewrite incident/report/evidence records.

The activity message is stored in the timeline table. The general audit log stores only activity ID, type and whether a location was supplied, avoiding a second copy of the message. A best-effort `incident.activity_added` event notifies operational WebSocket clients after commit. Redis event delivery failure does not roll back the committed activity.

## API

- `GET /api/incidents/{id}/activity?limit=50&offset=0`: newest-first page, total count and `has_more`. Limit 1–100; offset 0–10,000. The returned `next_cursor` (`before_created_at`, `before_id`) provides stable keyset pagination when new notes arrive while a client is paging; do not combine cursor fields with nonzero offset.
- `POST /api/incidents/{id}/activity`: accepts activity type, 10–3,000 nonblank message characters and optional location. Coordinates must be supplied as a valid pair.

Writes are rate-limited to 30 per user per minute. Entries are recorded in the normal audit log.

## Verification limits

Automated tests cover schema validation, role restrictions, closed-incident behavior, pagination, actor snapshots, audit metadata, migration behavior and endpoint workflows. Browser rendering, live Redis Pub/Sub delivery, PostgreSQL row-lock concurrency and actual responder procedures still need deployment-specific validation.
