# Event Evolution at Scale v1

Migration: `0022_event_evolution_scale`
Replay engine: `event-evolution-replay-v2-indexed`

## Objective

Reduce avoidable work in event-candidate discovery and historical replay without
weakening the existing complete-link rule, changing candidate scores, raising the
500-report replay limit, or changing human-review boundaries.

## Indexed complete-link clustering

The greedy clusterer now maintains a one-degree latitude/longitude grid keyed by
disaster type. Each candidate cluster is indexed by its first report (the anchor).
For a new report, the engine obtains a conservative spherical-cap bounding cover,
looks up only anchor cells that could be within the configured distance, and
removes anchors older than the configured time gap. The exact haversine distance,
temporal check, pair scorer and complete-link condition remain authoritative.

The index is a broad-phase filter only. It may return false positives, but it must
not prune a cluster that could pass the exact anchor constraints. Candidate cluster
indexes are evaluated in their original creation order, preserving deterministic
tie-breaking. Complete-link still requires the new report to match every member;
transitive chaining is not introduced.

Discovery metrics now record the number of possible cluster candidates, indexed
candidates, pruned candidates, pair requests, actual pair-score evaluations, cache
hits and accepted pairwise links. These are operational counters, not confidence
scores.

## Replay baseline narrowing

At replay request time, Cignal first finds active event hypotheses with at least one
association to a frozen replay input. It then freezes the complete active report
membership and metadata for each of those hypotheses, including members outside
the replay time window. Hypotheses with no possible overlap are omitted because
they cannot affect the replay's Jaccard comparison. Rejected associations remain
excluded, matching the previous baseline semantics.

A composite index on `(report_id, association_status, hypothesis_id)` supports the
initial overlap lookup. During replay, an inverted `report_id -> baseline indexes`
map limits Jaccard calculations to hypotheses that share at least one report with
the candidate cluster. Highest-overlap selection and deterministic tie-breaking
remain unchanged.

New replay requests pin engine version `event-evolution-replay-v2-indexed` and the
baseline scope in their immutable parameters. Existing replay rows retain their
original engine version and frozen data.

## Safety and correctness contract

- Candidate clustering output must match the brute-force greedy complete-link
  reference for the same reports, parameters and scoring implementation.
- Exact distance, time and pair-score gates remain unchanged.
- Replay still verifies each frozen input digest and aggregate input manifest.
- Baselines still freeze the full membership of every relevant hypothesis.
- Replay results are still candidate-only and never mutate canonical hypotheses,
  reports, incidents, warnings, response plans or operational actions.
- Semantic and supervised model scores remain disabled in deterministic replay.
- The existing 500-input and 366-day replay bounds remain in force. Dense local
  clusters still require pairwise checks; this phase does not claim sub-quadratic
  behavior for a single dense event.

## Verification

Tests compare indexed clusters against a brute-force reference, cover dateline and
polar spatial bounds, verify replay baseline narrowing and full-membership retention,
compare indexed replay matching with a full scan, and verify migration idempotency.
The release gate also runs the complete test suite, Ruff, Bandit, service health,
API contracts, persistent-storage checks and live-acquisition safety checks.
