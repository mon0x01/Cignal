# External ingestion normalization

External integrations are untrusted input boundaries. The worker normalizes source IDs before both lookup and insertion so the uniqueness check uses the exact same value written to PostgreSQL.

- Source record IDs up to 180 characters are retained verbatim after trimming.
- Longer IDs are represented by a readable 147-character prefix, a separator, and a 32-hex-character SHA-256 suffix. This keeps the database limit while avoiding collisions caused by naive truncation.
- Missing IDs use a deterministic hash of source type, title, coordinates and observation time.
- External location descriptions have whitespace collapsed and are bounded to 2,000 characters.
- `reports.location_text` is PostgreSQL `TEXT`, introduced by migration `0006_expand_report_location_text`.
- Incident and responder activity labels remain capped at 220 characters; incident creation copies a bounded label rather than copying an arbitrarily long source string.

Migration downgrade refuses to shrink `reports.location_text` if any stored value exceeds 220 characters. This avoids silently truncating or discarding source provenance.


## Reprocessing semantics

A report's `analysis_revision` represents the current report/evidence generation; `analyzed_revision` records the latest generation successfully analyzed. Changes to report text or evidence advance the requested generation. A worker may only commit an analysis when the database row still carries the requested generation. Stale work is discarded and the latest generation is re-enqueued. This protects against completed-job reuse and concurrent evidence/follow-up uploads.
