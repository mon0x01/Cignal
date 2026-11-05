# Data Acquisition and Source Governance v1

This increment adds two adapters and makes source-governance metadata part of each ingested report. It does not archive complete provider payloads and does not change the event-review or incident-confirmation workflow.

## Added sources

### NASA EONET v3

EONET supplies curated near-real-time metadata for natural events and links to source records and imagery. Cignal currently requests open events from the recent 30-day window (up to 200 records). Event categories and geometry are retained as bounded metadata. A Point is persisted as a point; a Polygon is represented by an approximate planar centroid for search/map indexing. This centroid is not a boundary and must not be treated as a precise affected-area estimate. EONET curation is not a severity or impact assessment, so Cignal does not infer urgency from EONET.

Official API documentation: https://eonet.gsfc.nasa.gov/docs/v3

### NDMA SACHET CAP-XML

Configure one or more SACHET feed/subscription identifiers using `SACHET_CAP_IDENTIFIERS`, separated by semicolons. Keep these identifiers private. Connector names and Redis cache keys use a one-way hash rather than printing the identifier.

The SACHET integration guide requires conditional requests with `ETag`: a `200` response supplies updated XML and its ETag; a `304` response means use the previously cached XML. Cignal follows this behavior and keeps the XML plus ETag in Redis for 30 days. If the ETag exists but the cached body is missing, it performs one unconditional recovery request. If Redis is unavailable, it fetches without ETag rather than accepting an unusable `304`.

Cignal parses CAP `Actual` messages and ignores `Exercise`, `System`, `Test`, `Ack` and `Error` messages. CAP `Cancel` messages are retained as explicitly titled low-urgency cancellation records; this is an audit trail, not a deletion of the earlier alert. Multiple `info` language blocks are handled by preferring English where available. A CAP polygon or circle may be reduced to an approximate point for indexing. The original area description is retained as text. Approximate points are not dispatch coordinates.

Official portal and integration guide: https://sachet.ndma.gov.in/CapFeed

## Source-governance metadata

Every ingested report receives a catalogue snapshot containing provider, dataset scope, access mode, licence/terms review status, attribution, retention policy, catalogue version and retrieval timestamp. Connector-specific metadata is bounded and secret-looking fields are removed. URL query credentials are stripped. Complete raw API responses are not stored in report metadata. Analyst pair labels also fingerprint this source-governance snapshot. If the governance metadata changes after annotation, the pair must be recreated against the current source record before the dataset can be sealed. Frozen dataset provenance and manifests include source licence-status counts and catalogue versions; licence review is not silently inferred from successful ingestion.

`license_status` is a workflow flag, not a legal determination. A configured RSS, social API, IMD, India government or Bhuvan endpoint must be reviewed against the exact provider and dataset terms. Dataset-level licences can differ even when the portal or publisher is the same. In particular, an API being reachable does not imply that all returned content can be retained or used to train models.

Weather context is catalogued as context-only and must not become an incident report. Satellite fire detections remain observations, not confirmed wildfires. EONET event metadata may point to imagery with separate terms; Cignal stores links and does not copy the underlying imagery.

## Configuration

```dotenv
NASA_EONET_ENABLED=true
SACHET_CAP_IDENTIFIERS=
```

Set `SACHET_CAP_IDENTIFIERS=identifier-one;identifier-two` only for feed identifiers that the operator is authorized to use. The identifier parser trims whitespace, removes duplicates, ignores empty/oversized/control-character values and caps the list at 50 feeds.

## Data model change

Migration `0013_source_provenance` adds a non-null JSON column `reports.source_metadata` with `{}` as its default. Existing rows are preserved and receive the empty object. No existing report/evidence is rewritten. New ingestion writes a source catalogue snapshot and bounded provider metadata with the report.

## Limits

- No adapter result is a confirmed incident; analysts retain review authority.
- The EONET connector fetches a bounded recent/open window. It is not a historical archive.
- SACHET XML is cached only to implement the provider's ETag protocol; it is not exported as a training dataset.
- Licence review and attribution obligations remain source-specific and must precede dataset export or model training.
- Real live-source behavior must be validated from the user's deployment. Unit tests use deterministic fixtures.
