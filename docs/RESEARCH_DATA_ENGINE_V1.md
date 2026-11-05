# Research & Dataset Governance Engine v1

## Purpose

The Research Data Engine creates a governed, reproducible path from Cignal's
existing ingested reports and analyst decisions to versioned event-pair training
and evaluation data. It does not fetch new providers or infer labels. Existing
connectors remain responsible for ingestion; this layer records dataset scope,
label provenance, independent review metadata, privacy clearance, immutable
versions and checksummed exports.

## Dataset lifecycle

1. An admin/analyst registers a dataset with its purpose, task, license/terms
   identifier, legal basis, consent scope and allowed source types.
2. An analyst labels a pair of existing report IDs as `same_event` or
   `different_event`, supplies an incident/disaster/campaign `split_group`, and
   records a rationale and label method.
3. An admin/analyst reviews the label and separately records a privacy review.
   Only `review_status=approved` and `privacy_review_status=passed` labels can
   enter a sealed version.
4. Dataset sealing freezes sanitized text, structured features, pair labels and
   source provenance into immutable version items. It computes canonical JSONL
   SHA-256 and a manifest containing licensing/governance metadata, class and
   group counts, source counts and redaction statistics.
5. A sealed version can be exported to JSONL and used by the v2 train/evaluate
   commands. Later label changes do not rewrite older versions.

## Label and split policy

- Report pairs are canonicalized by report ID, so reversed pairs collide.
- A pair can be labelled once per dataset. Conflicting labels require a new
  controlled dataset/version workflow rather than overwriting history.
- `split_group` means a complete disaster/event/campaign grouping, not a row ID.
  All pairs for the same real-world event must use the same group.
- Sealing requires at least 12 unique pairs, four independent groups, both
  classes, each class represented in at least two groups, and at least two
  groups containing both classes. The downstream trainer still validates that
  its chosen group holdout contains both classes.
- A source report ID or exact normalized report content cannot cross split
  groups. Identical text pairs cannot cross split groups, and identical pairs
  with conflicting labels are rejected. These checks reduce common leakage
  paths; they do not replace event-level grouping review or near-duplicate
  detection.
- Each label stores the report ID and a SHA-256 fingerprint of the exact title,
  description, disaster category, coordinates, observation/creation times,
  language and source metadata visible when the label was created. Sealing
  refuses a source report that changed after annotation, so a label cannot
  silently migrate to different training features.
- Versions are immutable snapshots. New labels or corrected adjudications are
  reflected only in a subsequently sealed version.

## Privacy and governance boundary

Dataset registration requires an explicit license/terms identifier, legal-basis
category, consent-scope category, purpose and source scope. These are governance
metadata supplied by the operator; Cignal does not make a legal determination
that a source's terms permit a particular use.

Pair approval requires an explicit privacy review. The dataset records a
coordinate precision policy (default: two decimal places, roughly kilometre
scale) and rounds report coordinates before export. Operators can choose a
precision from zero to four decimals according to the permitted research need;
more precise output requires careful privacy review. The export layer also
redacts obvious email addresses, Indian mobile numbers and international
`+country-code` phone patterns. This pattern-based pass is **not** a complete
PII detector and cannot reliably identify personal names, addresses, faces,
free-form identifiers or sensitive inferences. The API and snapshot builder
reject obvious email/phone patterns in split-group identifiers. The human privacy
review remains mandatory. Raw report text is not mutated; only the immutable training snapshot
contains the redacted copy. Reporter identities, account IDs, source URLs,
credentials and label rationales are not exported to JSONL.

The dataset registry records source types and a provenance snapshot (source
name/type and observation/creation times) separately from training text. Avoid
using personal names or identifiers in `split_group`; the API and snapshot
builder reject obvious email/phone patterns there.

## APIs

Admin/analyst-only:

- `POST /api/research/datasets`
- `GET /api/research/datasets`
- `GET /api/research/datasets/{dataset_id}`
- `POST /api/research/datasets/{dataset_id}/pair-labels`
- `PATCH /api/research/datasets/{dataset_id}/pair-labels/{label_id}/review`
- `POST /api/research/datasets/{dataset_id}/versions`
- `GET /api/research/datasets/{dataset_id}/versions`
- `GET /api/research/datasets/{dataset_id}/versions/{version_id}/export`

The export endpoint verifies the stored content hash before returning NDJSON.

## Export and train

Export into the existing shared model volume (persistent across API/worker
containers and included in the existing model-volume backup):

```bash
sudo docker compose exec -T --interactive=false api \
  python -m scripts.export_research_dataset DATASET_UUID VERSION_UUID \
  --output /data/models/datasets/gujarat-flood-pairs-v1.jsonl
```

Train the v2 event-pair ranker using the exported immutable version:

```bash
sudo docker compose exec -T --interactive=false api \
  python -m scripts.train_event_pair_model \
  /data/models/datasets/gujarat-flood-pairs-v1.jsonl \
  --output /data/models/event-pair-v2.joblib
```

Evaluate on a separate sealed dataset version with untouched event groups:

```bash
sudo docker compose exec -T --interactive=false api \
  python -m scripts.evaluate_event_pair_model \
  /data/models/datasets/untouched-final-holdout-v1.jsonl \
  --model /data/models/event-pair-v2.joblib
```

The export CLI refuses paths outside the shared `/data/models` volume and
verifies the immutable version SHA-256 before writing. It also writes a
`<dataset>.jsonl.manifest.json` sidecar containing the version, governance
metadata and content hash. Existing exports are never overwritten with
conflicting bytes; identical re-exports are idempotent. Model artifacts are
pickle/joblib based; only load artifacts created by the trusted Cignal training
workflow.

## Limitations

- No labelled operational dataset is bundled. The existing synthetic fixtures
  are software smoke tests, not performance evidence.
- Labels still require human adjudication and privacy review; this release does
  not autonomously label reports or determine legal authorization.
- Regex redaction catches only obvious email/phone patterns and is not exhaustive.
- Source provenance is preserved, but provider independence and source truth are
  not established by source labels.
- This release provides dataset governance and reproducible data snapshots, not
  a new disaster classifier or a claim that a model is better than existing
  pretrained models.
