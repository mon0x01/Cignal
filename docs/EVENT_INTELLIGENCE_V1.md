# Event Intelligence Engine v1

## Purpose

Event Intelligence v1 converts already-analyzed reports from independent source origins into persisted **event hypotheses**. A hypothesis is a candidate explanation that multiple observations may describe the same real-world event. It is not an Incident and it is not an emergency declaration.

## Data model

- `EventHypothesis` stores the event candidate, a stable key anchored to the earliest report, event-time range, centroid, source diversity, heuristic match score, scoring version and a machine-readable explanation.
- `EventHypothesisReport` stores a reversible, reviewable proposed/confirmed/rejected association between one report and one hypothesis. Pair-level component scores and reason codes are retained.
- `EventDiscoveryRun` records the requesting analyst, bounded parameters, job lifecycle, counts, duration and sanitized failure code.

Reports and existing Incidents are not mutated by automatic discovery. In particular, discovery never sets `Report.duplicate_of`, never creates an Incident, and never changes an Incident's verification or operational status.

## Candidate generation

The first version is deliberately conservative:

1. Only analyzed reports with supported disaster categories and valid coordinates are eligible.
2. Two reports must have the same known disaster category.
3. Their event/observation times must be within the configured window (default 72 hours).
4. Their coordinates must be within the configured radius (default 25 km).
5. The existing explainable report-pair scorer must meet the configured minimum heuristic match score (default 0.62).
6. A resulting cluster must contain at least two distinct source origins. An origin is `source_type:source_name`; independent citizen submissions currently share an origin label when their source metadata is identical, so they do not by themselves qualify as cross-source evidence.
7. Greedy complete-link grouping requires every pair in a cluster to pass. A-B and B-C matching does not allow A-B-C if A-C fails.

The defaults are engineering starting points, not calibrated thresholds. The score is explicitly a **heuristic match score, not a probability**. It has no demonstrated disaster-detection precision/recall yet.

## Workflow

- Admin/analyst submits `POST /api/intelligence/event-discoveries`.
- The request is recorded and queued to the RQ worker.
- The worker scans a bounded set (default 300) of analyzed reports and persists candidates.
- Discovery runs can be inspected under `/api/intelligence/event-discoveries`.
- Analysts inspect hypotheses and associated reports through `/api/intelligence/event-hypotheses`.
- A hypothesis can be moved to `investigating` or `dismissed`.
- Each report association can independently be confirmed or rejected. Human decisions are not overwritten by later automated discovery.
- Association review immediately refreshes the hypothesis report count, source diversity, spatial/temporal summary and heuristic pair score from the remaining non-rejected reports.

No automatic promotion to Incident exists in v1. Any future promotion action must use the existing incident review service and explicit human authorization.

## Known limitations and next steps

- Candidate generation is bounded and currently analyst-triggered; it is not yet a continuous streaming detector.
- Pairwise lexical similarity can underperform across languages, paraphrases, and terse alerts. Future work should add multilingual semantic retrieval while preserving evidence-based explanations.
- Spatial thresholds are global defaults; floods, earthquakes, fires and cyclones have different spatial/temporal footprints. Per-disaster policies need evaluation on labelled historical events.
- Source origin labels are not source reliability scores. Reliability must be learned/validated separately and must not imply truth.
- No threshold is calibrated. Build event-level labelled datasets and evaluate precision, recall, false merges, missed links, and calibration before operational use.
- The first algorithm uses greedy complete-link grouping. It is deterministic and conservative but can be order-sensitive; future versions should compare alternatives using held-out labelled clusters.
