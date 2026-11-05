# Event Intelligence Engine v2 — Semantic, Temporal and Source Intelligence

## Purpose

V2 extends the persisted event-hypothesis workflow from v1. It adds optional multilingual semantic similarity, event-state snapshots, source-operation/review profiles, and a supervised event-pair ranking pipeline that can be trained from operator-labelled JSONL pairs.

The operating boundary is unchanged: an `EventHypothesis` is an unverified analytical candidate. Discovery does not create an `Incident`, change incident status, dispatch responders, or set `Report.duplicate_of`. Analyst review remains authoritative.

## Components

### 1. Optional multilingual semantic embeddings

`app/intelligence/semantic.py` reuses the application's configured SentenceTransformer instance. It does not install a dependency or download weights. The model loader uses `local_files_only=True`; when the dependency is absent, the feature is disabled, or the model is not already staged locally, discovery continues with the explainable lexical baseline and records the reason in the discovery result.

Embeddings are persisted in `report_semantic_embeddings`, keyed by `(report_id, model_version)`. A SHA-256 digest includes normalized title, description, location text and language. Editing the report text invalidates its cache entry on the next discovery run. Similarities are cosine similarity values in `[0,1]`; they are not probabilities or truth estimates.

The default `AI_ENABLE_PRETRAINED_NLP=false` remains unchanged. To enable the feature, install the optional `requirements-ml.txt` dependencies in the API and worker images, stage the configured model files in a local Hugging Face cache or a shared local model directory, and set `AI_ENABLE_PRETRAINED_NLP=true`. Do not enable it until both inference processes can read the same model version. Model downloads are intentionally not triggered by a discovery job.

### 2. Supervised event-pair ranker

`app/intelligence/event_pair_model.py` loads an operator-trained, trusted joblib artifact from `EVENT_PAIR_MODEL_PATH`. The eight features are:

- token-set text similarity;
- character TF-IDF cosine similarity;
- token Jaccard similarity;
- numeric-token overlap;
- same known disaster category;
- geographic proximity;
- temporal proximity;
- same known language.

The model output is an **uncalibrated candidate-ranking score**, not a probability. It is blended with the explainable heuristic score while preserving the original component details and model version in the hypothesis explanation. The model never makes an operational decision.

The loader cache is keyed by path, modification time and file size, so an atomically replaced artifact becomes visible to long-running API/worker processes without relying on a process restart. Only load artifacts produced by the trusted Cignal training workflow. Joblib uses pickle-based serialization; loading an untrusted artifact can execute code.

### 3. Leakage-aware training and evaluation

Training accepts UTF-8 JSONL, one pair per line. Required fields:

```json
{"split_group":"event-or-campaign-id","text_a":"Report A text","text_b":"Report B text","same_event":1}
```

Optional fields include `title_a/b`, `disaster_type_a/b`, `latitude_a/b`, `longitude_a/b`, `observed_at_a/b` (ISO 8601), and `language_a/b`.

`split_group` is the incident, disaster or campaign grouping that must stay wholly on one side of the train/holdout split. It is **not** a row ID. The trainer uses `GroupShuffleSplit`; it fails closed unless both labels exist in both partitions. Duplicate pairs within a group are removed. At least 12 unique pairs, four independent groups and both labels are required.

The trainer reports precision, recall, F1, average precision, ROC AUC and a confusion matrix for its group-disjoint holdout. It also evaluates Cignal's existing heuristic `score_report_pair` on the exact same holdout and reports score deltas. The deployable artifact is then refit on all supplied training rows; its stored holdout metrics describe the earlier train/holdout experiment, not an independent evaluation of the refit artifact.

Use a second dataset with untouched event groups for the final evaluation command. Do not evaluate a model on the same dataset from which its deployable artifact was fitted and call that a holdout result. Synthetic fixtures are smoke tests only and must never be used to claim real-world quality.

The loader rejects the same exact text pair when it is assigned to multiple `split_group` values. This prevents a common form of pair-level split leakage; it does not detect every semantic or paraphrase duplicate, so dataset curation must still deduplicate related reports across groups.

The operator is responsible for label provenance, reviewer traceability, consent/legal basis, and removing direct personal data before exporting text. Do not export citizen identity, contact details, credentials, or private social data into model datasets. Prefer incident-level and time/geography-separated final holdouts to prevent near-duplicate leakage.

Example commands (run inside the API container; `data/training` is mounted read-only and `/data/models` is the shared model volume):

```bash
sudo docker compose exec -T --interactive=false api \
  python -m scripts.train_event_pair_model /data/training/event-pairs-train.jsonl \
  --output /data/models/event-pair-v1.joblib

sudo docker compose exec -T --interactive=false api \
  python -m scripts.evaluate_event_pair_model /data/training/event-pairs-final-holdout.jsonl \
  --model /data/models/event-pair-v1.joblib
```

The final-holdout file must contain event groups not present in the training file. The evaluator rejects overlap with `training_split_groups` recorded in the model artifact. Keep it separate from training data and do not use it for repeated tuning.

### 4. Event evolution snapshots

`event_hypothesis_snapshots` stores append-only snapshots of materially distinct hypothesis states: creation, expansion, rescoring and analyst review. Snapshots include report/association state, source origins, match score semantics, model versions, observation time range and geographic centroid. A unique state hash prevents identical repeated discovery runs from creating duplicate snapshots.

`GET /api/intelligence/event-hypotheses/{hypothesis_id}/timeline` returns the timeline newest-first. It is an analytical history, not an immutable external evidence ledger; the database operator still controls the underlying database and backups.

### 5. Source reliability profiles

`source_reliability_profiles` deliberately keeps two measurements separate:

- **Operational success:** successful ingestion runs divided by all recorded runs for the source origin.
- **Analyst association acceptance:** reviewed report-to-hypothesis associations confirmed by an analyst, summarized with a Beta(2,2) posterior mean, a Wilson 95% lower bound, and a sample-size confidence band.

The confidence bands are descriptive (`insufficient`, `low`, `moderate`, `higher_sample`) and are based on reviewed-association counts. Neither measure proves that a provider is truthful, complete, independent, unbiased or authoritative. A source can be operationally healthy but provide poor evidence; a source can also be technically unreliable while some individual observations are correct. These metrics are not yet used as automatic weights in event matching.

Profiles are materialized by an admin/analyst refresh endpoint and refreshed after an analyst reviews an association. No claim is made that provider-source identities are independent merely because their labels differ.

## Database changes

Migration `0011_event_intel_v2` adds:

- `report_semantic_embeddings` — content-addressed report embedding cache;
- `event_hypothesis_snapshots` — event evolution history;
- `source_reliability_profiles` — source operation/review aggregates;
- `event_discovery_runs.result_summary` — semantic/model execution summary.

The migration is guarded for the expected schema and has SQLite idempotency coverage. PostgreSQL migration and live queue behavior must still be verified in the target Compose deployment.

## V2 API

Admin/analyst-only:

- `GET /api/intelligence/semantic-status` — reports optional embedding and supervised model availability/versions.
- `GET /api/intelligence/source-reliability?limit=100` — lists materialized profiles.
- `POST /api/intelligence/source-reliability/refresh` — recomputes profiles from persisted ingestion runs and analyst decisions.
- `GET /api/intelligence/event-hypotheses/{hypothesis_id}/timeline` — retrieves append-only hypothesis snapshots.

Existing v1 discovery and review routes continue to operate. The discovery run result includes semantic-provider status, cache statistics and supervised-model status.

## Current limitations and acceptance criteria

- No representative, independently labelled disaster-event pair dataset is bundled. The test fixture is synthetic and only validates software behavior.
- No empirical claim is made that the learned ranker outperforms the heuristic baseline on real disaster data.
- SentenceTransformer support is optional and off by default; multilingual quality, domain adaptation, latency and memory use need measurement on real data and target hardware.
- Source-reliability profiles are descriptive and are not used to weight scores automatically.
- Event snapshots are limited to states observed by Cignal discovery/review runs; they do not reconstruct unobserved real-world event evolution.
- The bounded discovery process is analyst-triggered. It is not a continuous stream processor or a validated early-warning system.
- Confidence calibration, geographic/language stratification, adversarial robustness, provider-independence analysis and operational pilot evaluation remain future work.

Before claiming model improvement, report group-disjoint train/validation results and a separate untouched test set with class-specific precision/recall, macro-F1, average precision, ROC AUC, calibration analysis, false-positive/false-negative review and geographic/language coverage. Compare against the same frozen heuristic baseline.
