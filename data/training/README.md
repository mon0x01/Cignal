# Operator-labelled training data

Place separately reviewed, privacy-screened JSONL event-pair datasets here.
Compose mounts this directory read-only at `/data/training` in API and worker
containers. Do not place raw citizen identity, contact information, credentials,
or private social-media content in training exports.

Use the schema in `../evaluation/event_pairs.template.jsonl`. Each `split_group`
must identify the incident/disaster/campaign grouping. Exact text-pair duplicates
across different groups are rejected to prevent leakage. Keep a final evaluation
file in separate, untouched event groups and never use it for tuning.

No labelled operational dataset is bundled. Synthetic fixtures are test data only.
