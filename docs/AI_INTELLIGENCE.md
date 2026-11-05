# Cignal AI and Multimodal Intelligence

## Scope and safety boundary

Cignal runs an advisory, versioned analysis pipeline. It does not declare emergencies, verify incidents, dispatch responders, or autonomously merge reports. Analyst review remains authoritative. Scores are uncalibrated model outputs, not probabilities or proof.

## Processing flow

1. The ingestion worker loads normalized report text and evidence metadata.
2. The NLP component predicts one of flood, earthquake, fire, cyclone, landslide, or other; extracts keyword signals and known-city coordinates; and records model/method metadata.
3. Image evidence is checked against the upload-time SHA-256 and byte count before decoding. The CPU-first vision component extracts dimensions, format, average hash, brightness, contrast, entropy, sharpness and quality warnings. It extracts only the GPS coordinate fields already safely retained during upload; it does not preserve raw EXIF camera metadata.
4. Image near-duplicate and report near-duplicate results are candidates only. No report or evidence is deleted or merged.
5. Location conflicts are preserved for human review. Submitted coordinates take priority over EXIF GPS; EXIF-derived coordinates are labelled as requiring confirmation.
6. A versioned JSON result is persisted to `reports.ai_analysis`; analysts/admins can retrieve it through `GET /api/reports/{report_id}/analysis`.

## Model limitations

The built-in text classifier is a lightweight baseline trained on a small curated multilingual phrase set. Its scores are uncalibrated and it must not be represented as validated against a real disaster corpus. For meaningful quality, collect a legally usable, human-labelled, representative dataset and measure per-class precision, recall, macro-F1, confusion matrices, geographic/language coverage and calibration on a held-out set.

The default image component is deterministic quality/integrity analysis. Optional Torchvision ImageNet labels provide generic visual context only; they are **not** a disaster damage detector. A separate CPU-first HOG + RGB classifier can be trained from operator-labelled images and will return candidate labels with evaluation metrics. It is a classical computer-vision baseline, not a state-of-the-art deep damage detector; validate it against a representative, independently collected disaster dataset before operational use. Do not use it to infer casualties, identify people, estimate structural safety or authorize response.

Optional SentenceTransformers and Torchvision adapters are disabled by default. The default pipeline runs on CPU and does not download model weights at runtime.

## Train a local text model

Training is offline and requires operator-supplied labels. The accepted CSV schema is either:

```csv
category,text
flood,"River water overflowed into homes after heavy rainfall near the bridge."
earthquake,"A strong earthquake tremor shook buildings and cracked the walls."
```

or `category,title,description`. Supported labels: `flood`, `earthquake`, `fire`, `cyclone`, `landslide`, `other`. Each class needs at least five unique examples; use hundreds or thousands per class for a useful model, and avoid mixing near-duplicate examples across train and holdout splits.

Put the reviewed CSV at `data/training/disaster-labels.csv`. The directory is mounted read-only in the API container; trained artifacts are written to the shared persistent `models_data` volume. Example:

```bash
sudo docker compose exec -T api python scripts/train_text_model.py   /data/training/disaster-labels.csv --min-accuracy 0.70
```

The trainer performs a stratified holdout evaluation, prints accuracy and macro-F1, and refuses export below the configured accuracy threshold. This is a simple initial gate, not a substitute for a robust dataset protocol. Joblib artifacts are pickle-based: load only artifacts produced by this trusted training workflow; never place untrusted model files in `/data/models`.

For operator-labelled image data, use the same supported categories as directory names:

`flood_water`, `fire_smoke`, `structural_damage`, `landslide_debris`, `blocked_road`, `unaffected`, `other`.

Arrange the dataset as `data/training/images/<category>/*`. Each category requires at least 10 unique images; the trainer uses a stratified holdout, reports accuracy and macro-F1, and will not export below the minimum accuracy. Example:

```bash
sudo docker compose exec -T api python scripts/train_vision_model.py \
  /data/training/images --min-accuracy 0.60
```

The HOG/RGB feature extractor is deterministic and CPU-bounded. Its prediction is stored as an unverified candidate and never changes report category, urgency, incident status or dispatch state.

After export, restart the inference processes so each process loads the artifact at startup:

```bash
sudo docker compose restart api worker
```

The API and worker share `/data/models`. Text and image training CSV/folders are mounted read-only at `/data/training`. New backups include `models.tar.gz`, its checksum, and an artifact list in `manifest.json`; restore supports both new backups and older backups that did not contain model artifacts.

## Event-pair intelligence models

Event Intelligence v2 optionally uses multilingual SentenceTransformer embeddings and an operator-trained report-pair ranker. Embeddings are cached by report content digest and model version. Pretrained NLP remains disabled by default and loads only from an already available local model cache or directory (`local_files_only=True`); discovery never initiates a weight download.

The event-pair trainer accepts group-labelled JSONL and uses an incident/campaign-disjoint holdout. It reports both the learned ranker's metrics and the existing heuristic `score_report_pair` baseline on the same holdout. It then refits the deployable artifact on all supplied training rows, so final independent evaluation must use event groups that were not included in that file. Synthetic test fixtures are not evidence of real-world model quality. The full data format, commands, source-profile interpretation and model limitations are documented in `docs/EVENT_INTELLIGENCE_V2.md`.

## Image limits

Image processing is bounded to `AI_MAX_IMAGES_PER_REPORT` (default 8, hard-capped at 32) images per report and 40 million pixels per image. PDFs and other non-image files are not passed to the vision analyzer. Evidence is never transformed by the analyzer; it reads the stored object.

## External source identity and location normalization

External source IDs are normalized to the report column's 180-character limit. IDs exceeding that limit retain a readable prefix plus a SHA-256 suffix so distinct long identifiers do not collapse to the same truncated value. External place descriptions are whitespace-normalized and bounded to 2,000 characters. Report location text uses PostgreSQL `TEXT`; incident and responder activity location labels remain bounded to 220 characters. This keeps source detail without allowing external feeds to break ingestion on a varchar limit.
