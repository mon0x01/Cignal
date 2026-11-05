# Report Discovery and Pagination

## Why this exists

The original report-review screen fetched only the newest 150 records. Older records remained in PostgreSQL but were not discoverable from that screen. The new search API and UI remove that fixed-window limitation.

## API

`GET /api/reports/search`

Query parameters:

| Parameter | Meaning | Constraints |
|---|---|---|
| `q` | Case-insensitive search across tracking code, title, description, location, source name and source record ID | Up to 120 characters |
| `status_filter` | Filter by report workflow status | `submitted`, `analyzed`, `needs_info`, `verified`, `rejected` |
| `source_type` | Exact source-type filter | Up to 24 characters |
| `disaster_type` | Exact disaster-type filter | Up to 32 characters |
| `date_from` | Inclusive UTC calendar date | ISO date, `YYYY-MM-DD` |
| `date_to` | Inclusive UTC calendar date | ISO date, `YYYY-MM-DD` |
| `sort` | Sort order | `newest`, `oldest`, `urgency` |
| `page` | 1-based page number | At least 1 |
| `page_size` | Items per page | 1–100; default 25 |

The response contains `items`, `total`, `page`, `page_size`, `total_pages`, `has_previous` and `has_next`. Ordering includes a stable ID tie-breaker so records with equal timestamps do not randomly move between pages.

The existing `GET /api/reports` endpoint remains unchanged and continues returning a list for existing clients. New clients should use `/api/reports/search`.

## Authorization

- Administrators and analysts can search all reports, including anonymous citizen submissions.
- Citizens can search only reports whose `submitted_by` matches their own user ID.
- Responders receive HTTP 403 from report search and should use the verified incident endpoints.

Search does not expose reporter identity. The report serializer continues returning `is_anonymous`, not `submitted_by`.

## UI

The Report review page supports text search, status/source/disaster filters, inclusive UTC date filters, sorting, selectable page size and previous/next navigation. Applying filters starts at page 1. Clearing filters restores the default newest-first view.

## Performance notes

The endpoint counts matching records and returns at most 100 per page. Existing indexes support creation-time and workflow-status ordering/filtering. Text search currently uses case-insensitive substring matching (`ILIKE` on PostgreSQL); that is appropriate for the current dataset but will need PostgreSQL trigram/full-text indexes if the report corpus grows substantially. Do not add a broad unindexed search to a high-volume production database without measuring query plans.

## Validation

The API workflow tests cover anonymous-report discovery, text/source/type/status filters, pagination without overlap, urgency sorting, out-of-range page clamping, invalid date ranges, citizen data isolation and responder denial. The UI source is syntax-compiled; full browser rendering should be verified on the running deployment.
