# Event Evolution & Historical Replay v1

## Purpose

Replay event-correlation rules over a bounded historical window using an immutable input manifest. The replay produces candidate clusters and compares them with the current event-hypothesis association sets. It does not modify reports, event hypotheses, incidents, response plans, warnings, or operational actions.

## Data model

- `event_replay_runs`: request window, pinned engine/scoring versions, input-manifest digest, status and summary.
- `event_replay_inputs`: frozen, minimal report fields consumed by the correlation algorithm, with a per-input SHA-256 digest.
- `event_replay_baselines`: frozen report-membership sets and hypothesis metadata captured when the replay is requested.
- `event_replay_results`: candidate cluster, report membership, scores, comparison classification and explainability details.

The frozen payload contains only fields required by the correlation scorer. It is stored in the existing protected PostgreSQL application database and follows the same access controls and database backup policy as report data. Replay retention and deletion controls should be added before large-scale production use.

## Determinism contract

A replay pins both its input set and the comparison baseline as of request time. A replay pins:

- engine version (`event-evolution-replay-v1` for original runs; `event-evolution-replay-v2-indexed` for new indexed runs),
- event scoring version,
- scoring thresholds and spatial/temporal bounds,
- exact input report payloads through input and aggregate manifest hashes.

Semantic embeddings and supervised pair-model scores are disabled for this replay version. This avoids mutable external model artifacts affecting a replay. A future model-enabled replay must pin model artifact hashes and store all preprocessing metadata.

## Comparison semantics

Each replay cluster is compared with current event hypotheses using report-ID Jaccard overlap:

- `new_candidate`: no report overlap,
- `unchanged`: exact report membership,
- `expanded`: replay cluster contains all current reports and adds members,
- `contracted`: replay cluster is a strict subset of current membership,
- `diverged`: partial overlap without set containment.

These are comparative labels, not probabilities or declarations of event identity. The highest-overlap hypothesis is selected deterministically; ties are resolved by overlap count and hypothesis ID. The v2 engine uses a report-to-hypothesis inverted index to calculate overlap only for hypotheses sharing at least one input report. At request time it freezes only potentially overlapping hypotheses, but preserves each selected hypothesis's complete active report membership so Jaccard semantics do not change.

## API

- `POST /api/intelligence/event-replays` — queue a replay and freeze inputs in the same transaction as the durable outbox request.
- `GET /api/intelligence/event-replays` — list recent replays.
- `GET /api/intelligence/event-replays/{run_id}` — inspect run status and manifest metadata.
- `GET /api/intelligence/event-replays/{run_id}/results` — page through replay candidates; optional comparison-kind filter.

Only administrators and analysts can access these endpoints. Requests are rate-limited and bounded to 500 reports and 366 days.

## Reliability and safety

- Replay request and outbox enqueue are committed atomically.
- The worker validates every frozen input hash and the aggregate manifest before processing.
- Results are deterministic for the same frozen inputs, parameters and pinned scoring version.
- Re-executing a completed run is an idempotent no-op.
- Replay writes only replay run/result tables. It does not auto-merge hypotheses or create incidents.
- The durable outbox currently guarantees durable publication attempts; RQ worker execution remains at-least-once and must tolerate duplicate delivery.

## Known limitations

- Replay is bounded to 500 reports to constrain pairwise clustering complexity. The indexed broad phase prunes spatially/temporally impossible cluster anchors; a single dense event can still require quadratic pairwise checks.
- Replay output is research/analyst decision support and requires human review. See `docs/EVENT_EVOLUTION_SCALE_V1.md` for the indexed discovery and replay implementation.
