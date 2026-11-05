# API overview

The FastAPI OpenAPI document is available in development at `/api/docs` and `/api/openapi.json`. In `APP_ENV=production`, interactive documentation and the OpenAPI JSON endpoint are disabled by default.

## Main endpoints

| Method | Path | Purpose / access |
|---|---|---|
| GET | `/api/health` | Database and Redis health |
| GET | `/api/setup/status` | Whether first administrator setup remains available |
| POST | `/api/setup/admin` | One-time initial admin setup |
| POST | `/api/auth/login` | Password + TOTP (once enrolled) |
| POST | `/api/auth/refresh` | Refresh-token rotation |
| POST | `/api/auth/logout` | Revoke supplied refresh token |
| POST | `/api/auth/mfa/start` | Generate TOTP enrollment secret |
| POST | `/api/auth/mfa/verify` | Verify TOTP and issue one-time recovery codes |
| POST | `/api/reports` | Create report; supports optional bearer auth and `Idempotency-Key` |
| GET | `/api/reports` | User-owned reports for citizens; reports for analysts/admins |
| POST | `/api/reports/{id}/evidence` | Add validated evidence to a report |
| POST | `/api/reports/{id}/review` | Analyst/admin verify, link, reject or request more information |
| GET | `/api/incidents` | Verified/monitoring incident list; admin/analyst/responder |
| GET | `/api/incidents/nearby` | Geographic radius query; PostGIS on PostgreSQL |
| PATCH | `/api/incidents/{id}` | Analyst/admin update severity, summary or lifecycle status |
| GET | `/api/incidents/{id}/activity` | Paginated operational timeline; admin/analyst/responder |
| POST | `/api/incidents/{id}/activity` | Append an operational note/update; admin/analyst/responder; closed incidents reject new entries |
| POST | `/api/import/historical/csv` | Analyst/admin import historical UTF-8 CSV |
| GET | `/api/analytics/summary` | Aggregated stored-record counts; admin/analyst |
| GET | `/api/weather/forecast` | Live Open-Meteo context; admin/analyst/responder |
| GET | `/api/integrations` | Connector configuration status; admin/analyst |
| POST | `/api/integrations/ingest` | Queue connector ingestion; admin/analyst |
| GET/POST/DELETE | `/api/push/*` | Optional Web Push registration |
| GET/POST/PATCH | `/api/users*` | Admin-only user and MFA administration |
| GET | `/api/audit` | Admin-only audit history |
| WebSocket | `/api/ws/incidents?token=...` | Authenticated live incident/notification events |

All protected endpoints enforce backend roles. Hiding a UI navigation item is not considered authorization. Citizen report access is restricted to reports associated with that citizen's account; anonymous users receive a tracking code but no account-based report list.

## Report and incident workflow

`POST /api/reports` returns a tracking code and creates a `submitted` report. A background worker adds AI category/urgency/location hints and changes `submitted` to `analyzed` where appropriate. An analyst can then choose `verify`, `link`, `reject` or `request_info`. Verification creates or links an `Incident` and adds a `report_incidents` relationship. AI does not create a verified incident.

`Idempotency-Key` is optional. When supplied, it is stored under a unique constraint and repeated calls return the original report. Offline draft retries reuse the same idempotency key.

## Incident operational activity

The incident activity timeline is append-only through the API. Operational roles can record `observation`, `situation_update`, `resource_request`, `access_constraint`, `handover` or `note`. Each entry records the author's display-name and role snapshots, timestamp, message, and optional location. Latitude and longitude must be supplied together. Responders can add activity but cannot change incident severity or lifecycle status. Closed incidents are read-only. Activity creation is audited and publishes an `incident.activity_added` realtime event; audit metadata does not duplicate the message body.

`GET /api/incidents/{id}/activity?limit=50&offset=0` returns newest entries first with `total` and `has_more`. Limit is 1–100 and offset is capped at 10,000. For stable traversal while new activity is being posted, use the returned `next_cursor` as the next request's `before_created_at` and `before_id`; cursor pagination cannot be combined with a nonzero offset. `POST /api/incidents/{id}/activity` accepts `activity_type`, a 10–3,000-character nonblank `message`, and optional `location_text`, `latitude`, and `longitude`. Writes are rate-limited per user.

## Historical CSV schema

Required headers: `title`, `description`.

Optional: `disaster_type`, `urgency`, `latitude`, `longitude`, `location_text`, `language`, `source_url`.

UTF-8/UTF-8-BOM, maximum 5 MB and 5,000 accepted rows per request. Imported records have source type/provenance `historical`. The sample file is `data/historical_sample.csv`.

## AI assessment

`GET /api/reports/{report_id}/analysis` returns the persisted versioned AI analysis for administrator and analyst roles. It returns 404 when analysis has not been generated. AI outputs are advisory and require human review. See [AI Intelligence](AI_INTELLIGENCE.md).


## Integration run observability

- `GET /api/integrations/health` (admin/analyst): connector configuration plus latest persisted run outcome, last successful time and 24-hour counts.
- `GET /api/integrations/runs?source_type=usgs&status_filter=failed&limit=25` (admin/analyst): recent persisted connector runs, including `fetch_attempts`. `limit` is bounded to 1–100.
- `POST /api/integrations/ingest` (admin/analyst): queues a manual ingestion run; the worker records per-source outcomes.

Provider event time is exposed as `source_observed_at` on report responses. It is distinct from `created_at`, which remains Cignal's ingestion/submission time.


## Event Intelligence v1

Admin/analyst routes:

- `POST /api/intelligence/event-discoveries` — queue bounded event hypothesis discovery.
- `GET /api/intelligence/event-discoveries` and `GET /api/intelligence/event-discoveries/{run_id}` — inspect discovery runs.
- `GET /api/intelligence/event-hypotheses` and `GET /api/intelligence/event-hypotheses/{hypothesis_id}` — inspect hypotheses and report associations.
- `PATCH /api/intelligence/event-hypotheses/{hypothesis_id}/review` — mark a hypothesis investigating or dismissed.
- `PATCH /api/intelligence/event-hypotheses/{hypothesis_id}/reports/{report_id}` — confirm or reject an individual report association.

Scores are heuristic match scores, not probabilities. Automated discovery never creates or verifies Incidents. See `docs/EVENT_INTELLIGENCE_V1.md`.

## Event Intelligence v2

Admin/analyst routes:

- `GET /api/intelligence/semantic-status` — optional multilingual embedding and supervised event-pair model status/version.
- `GET /api/intelligence/event-hypotheses/{hypothesis_id}/timeline?limit=100` — append-only hypothesis state snapshots, newest first.
- `GET /api/intelligence/source-reliability?limit=100` — separate operational-ingestion and analyst-review aggregates by source origin.
- `POST /api/intelligence/source-reliability/refresh` — recompute source profiles from stored integration runs and analyst association decisions.

Discovery results include embedding cache/inference status, configured model version and supervised-pair-model status. SentenceTransformer inference is optional, off by default and local-files-only; a discovery job does not download model weights. The trained pair ranker is an uncalibrated candidate-ranking score. Source acceptance is not source truthfulness. See `docs/EVENT_INTELLIGENCE_V2.md` for dataset format, group-disjoint evaluation, baseline comparison, model artifact policy and limitations.

## Research dataset governance

Admin/analyst-only research routes register datasets with an explicit license/terms
identifier, legal basis, consent scope, purpose and allowed source types; collect
reviewable `same_event`/`different_event` pair labels; record independent label
and privacy review state; seal immutable dataset versions; and export checksum-
verified JSONL. See `docs/RESEARCH_DATA_ENGINE_V1.md` for request lifecycle,
required label fields, export commands and limitations.

Dataset registration accepts `coordinate_precision` from 0 to 4 decimals (default 2); the chosen precision is frozen into each version manifest.


## Report source provenance

Report response objects include `source_metadata`, a bounded source-catalogue snapshot containing provider/dataset scope, access mode, licence/terms review status, attribution, retention policy, catalogue version and retrieval timestamp. Provider-specific identifiers and metadata are included only where safe and bounded; complete raw provider payloads are not stored. Citizen submissions carry a consent-scope review flag.
