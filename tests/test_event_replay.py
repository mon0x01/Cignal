from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    BackgroundJobOutbox,
    Base,
    EventHypothesis,
    EventHypothesisReport,
    EventReplayBaseline,
    EventReplayInput,
    EventReplayResult,
    Report,
)
from app.services.event_replay import create_event_replay, execute_event_replay


def _report(source: str, observed: datetime, *, offset: float = 0.0) -> Report:
    return Report(
        id=str(uuid4()),
        tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
        title="Flood water rising near Ahmedabad central market",
        description="Heavy flood water is entering homes and residential roads near the central market in Ahmedabad.",
        disaster_type="flood",
        urgency="high",
        status="analyzed",
        latitude=23.0225,
        longitude=72.5714 + offset,
        location_text="Ahmedabad, Gujarat",
        language="en",
        source_type=source,
        source_name=source.upper(),
        provenance="live",
        source_observed_at=observed,
        analysis_revision=1,
        analyzed_revision=1,
        created_at=observed,
        updated_at=observed,
    )


@pytest.fixture

def db_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_replay_freezes_bounded_inputs_and_durable_job_atomically(db_factory):
    now = datetime.now(UTC)
    with db_factory() as db:
        db.add_all([
            _report("usgs", now - timedelta(hours=2)),
            _report("gdacs", now - timedelta(hours=1), offset=0.001),
        ])
        db.commit()
        run, job = create_event_replay(
            db, requested_by=None, start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1)
        )
        db.commit()
        assert run.status == "queued"
        assert run.input_count == 2
        assert len(run.input_manifest_sha256) == 64
        assert db.query(EventReplayInput).filter_by(run_id=run.id).count() == 2
        assert run.baseline_count == 0
        assert db.query(EventReplayBaseline).filter_by(run_id=run.id).count() == 0
        outbox = db.query(BackgroundJobOutbox).filter_by(job_id=job.job_id).one()
        assert outbox.status == "pending"
        assert outbox.function_path == "app.worker.replay_event_history"
        from app.services.job_outbox import _ALLOWED_FUNCTIONS
        assert outbox.function_path in _ALLOWED_FUNCTIONS


def test_replay_is_deterministic_and_never_mutates_canonical_hypotheses(db_factory):
    from app.intelligence.event_hypotheses import SCORING_VERSION

    now = datetime.now(UTC)
    with db_factory() as db:
        db.add_all([
            _report("usgs", now - timedelta(hours=2)),
            _report("gdacs", now - timedelta(hours=1), offset=0.001),
        ])
        db.commit()
        run, _job = create_event_replay(
            db, requested_by=None, start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1)
        )
        db.commit()
        before = db.query(EventHypothesis).count()
        result = execute_event_replay(db, run.id)
        db.commit()
        assert result["ok"] is True
        assert result["candidate_count"] == 1
        assert result["canonical_events_mutated"] is False
        assert db.query(EventHypothesis).count() == before
        row = db.query(EventReplayResult).filter_by(run_id=run.id).one()
        assert row.comparison_kind == "new_candidate"
        assert row.report_count == 2
        assert row.explanation["canonical_state_mutated"] is False
        assert run.scoring_version == SCORING_VERSION
        second = execute_event_replay(db, run.id)
        assert second["idempotent_replay"] is True
        assert db.query(EventReplayResult).filter_by(run_id=run.id).count() == 1


def test_replay_compares_exact_report_set_to_current_hypothesis(db_factory):
    now = datetime.now(UTC)
    with db_factory() as db:
        reports = [
            _report("usgs", now - timedelta(hours=2)),
            _report("gdacs", now - timedelta(hours=1), offset=0.001),
        ]
        db.add_all(reports)
        db.flush()
        hypothesis = EventHypothesis(
            id=str(uuid4()), event_key=uuid4().hex, anchor_report_id=reports[0].id,
            title="Flood event candidate", summary="Candidate", disaster_type="flood",
            status="candidate", centroid_latitude=23.0225, centroid_longitude=72.5714,
            first_observed_at=now - timedelta(hours=2), last_observed_at=now - timedelta(hours=1),
            report_count=2, distinct_source_count=2, match_score=0.9,
            scoring_version="legacy-v1", explanation={},
        )
        db.add(hypothesis)
        for report in reports:
            db.add(EventHypothesisReport(
                id=str(uuid4()), hypothesis_id=hypothesis.id, report_id=report.id,
                association_status="confirmed", relationship_type="supports",
                pair_score=0.9, component_scores={}, reasons=[],
            ))
        db.commit()
        run, _job = create_event_replay(
            db, requested_by=None, start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1)
        )
        db.commit()
        assert run.baseline_count == 1
        assert db.query(EventReplayBaseline).filter_by(run_id=run.id).count() == 1
        # Change the live association after request; replay must compare to the frozen baseline.
        for association in db.query(EventHypothesisReport).filter_by(hypothesis_id=hypothesis.id):
            association.association_status = "rejected"
        reports[0].title = "The live report has since been edited"
        db.commit()
        execute_event_replay(db, run.id)
        db.commit()
        result = db.query(EventReplayResult).filter_by(run_id=run.id).one()
        assert result.comparison_kind == "unchanged"
        assert result.matched_hypothesis_id == hypothesis.id
        assert result.overlap_score == 1.0


def test_replay_detects_tampered_frozen_input(db_factory):
    now = datetime.now(UTC)
    with db_factory() as db:
        db.add_all([
            _report("usgs", now - timedelta(hours=2)),
            _report("gdacs", now - timedelta(hours=1), offset=0.001),
        ])
        db.commit()
        run, _job = create_event_replay(
            db, requested_by=None, start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1)
        )
        db.commit()
        item = db.query(EventReplayInput).filter_by(run_id=run.id).first()
        item.input_data["title"] = "tampered"
        db.commit()
        with pytest.raises(ValueError, match="replay_input_integrity_failure"):
            execute_event_replay(db, run.id)


def test_replay_request_rejects_invalid_time_window(db_factory):
    now = datetime.now(UTC)
    with db_factory() as db:
        with pytest.raises(ValueError, match="start_at must be earlier"):
            create_event_replay(
                db, requested_by=None, start_at=now, end_at=now - timedelta(seconds=1)
            )


def test_replay_baseline_freezes_only_overlapping_hypotheses_with_full_membership(db_factory):
    now = datetime.now(UTC)
    with db_factory() as db:
        in_window_a = _report("usgs", now - timedelta(hours=2))
        in_window_b = _report("gdacs", now - timedelta(hours=1), offset=0.001)
        outside_window = _report("imd", now - timedelta(days=5), offset=0.002)
        unrelated = _report("rss", now - timedelta(days=6), offset=0.003)
        db.add_all([in_window_a, in_window_b, outside_window, unrelated])
        db.flush()

        hypothesis = EventHypothesis(
            id=str(uuid4()), event_key=uuid4().hex, anchor_report_id=in_window_a.id,
            title="Relevant flood candidate", summary="Candidate", disaster_type="flood",
            status="candidate", centroid_latitude=23.0225, centroid_longitude=72.5714,
            first_observed_at=now - timedelta(days=5), last_observed_at=now - timedelta(hours=1),
            report_count=3, distinct_source_count=3, match_score=0.9,
            scoring_version="legacy-v1", explanation={},
        )
        irrelevant_hypothesis = EventHypothesis(
            id=str(uuid4()), event_key=uuid4().hex, anchor_report_id=unrelated.id,
            title="Unrelated candidate", summary="Candidate", disaster_type="flood",
            status="candidate", centroid_latitude=23.0225, centroid_longitude=72.5714,
            first_observed_at=now - timedelta(hours=3), last_observed_at=now - timedelta(hours=3),
            report_count=1, distinct_source_count=1, match_score=0.0,
            scoring_version="legacy-v1", explanation={},
        )
        db.add_all([hypothesis, irrelevant_hypothesis])
        for report in (in_window_a, in_window_b, outside_window):
            db.add(EventHypothesisReport(
                id=str(uuid4()), hypothesis_id=hypothesis.id, report_id=report.id,
                association_status="confirmed", relationship_type="supports",
                pair_score=0.9, component_scores={}, reasons=[],
            ))
        db.add(EventHypothesisReport(
            id=str(uuid4()), hypothesis_id=irrelevant_hypothesis.id, report_id=unrelated.id,
            association_status="confirmed", relationship_type="supports",
            pair_score=0.9, component_scores={}, reasons=[],
        ))
        db.commit()

        run, _job = create_event_replay(
            db, requested_by=None,
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(minutes=1),
        )
        db.commit()

        baselines = db.query(EventReplayBaseline).filter_by(run_id=run.id).all()
        assert run.baseline_count == 1
        assert [row.hypothesis_id for row in baselines] == [hypothesis.id]
        assert set(baselines[0].report_ids) == {
            in_window_a.id, in_window_b.id, outside_window.id,
        }
        assert run.parameters["baseline_scope"] == "overlap_relevant_hypotheses_with_full_membership"


def test_indexed_baseline_comparison_matches_full_scan():
    from types import SimpleNamespace

    from app.services.event_replay import _comparison

    baselines = [
        (SimpleNamespace(id="hyp-a", status="candidate", scoring_version="v1"), {"r1", "r2"}),
        (SimpleNamespace(id="hyp-b", status="investigating", scoring_version="v2"), {"r2", "r3", "r4"}),
        (SimpleNamespace(id="hyp-c", status="candidate", scoring_version="v1"), {"unrelated"}),
    ]
    report_index = {"r1": {0}, "r2": {0, 1}, "r3": {1}, "r4": {1}, "unrelated": {2}}

    for candidate in ({"r1", "r2"}, {"r2", "r3"}, {"r8"}):
        full_scan = _comparison(candidate, baselines)
        diagnostics = {}
        indexed = _comparison(candidate, baselines, report_index=report_index, diagnostics=diagnostics)
        assert indexed[:3] == full_scan[:3]
        indexed_detail = dict(indexed[3])
        full_detail = dict(full_scan[3])
        indexed_detail.pop("baseline_candidates_considered", None)
        full_detail.pop("baseline_candidates_considered", None)
        assert indexed_detail == full_detail
        assert diagnostics["baseline_candidates_considered"] <= len(baselines)
    assert _comparison({"r8"}, baselines, report_index=report_index)[0] == "new_candidate"
