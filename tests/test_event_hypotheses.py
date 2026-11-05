from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, inspect

from app.db.models import Base, EventHypothesis, EventHypothesisReport, Report
from app.intelligence.event_hypotheses import (
    SCORING_VERSION,
    build_candidate_clusters,
    discover_event_hypotheses,
)


def _report(
    report_id: str,
    source_type: str,
    *,
    source_name: str | None = None,
    longitude: float = 72.5714,
    observed_at: datetime | None = None,
    disaster_type: str = "flood",
    title: str = "Flood water rising near Ahmedabad central market",
    description: str = (
        "Heavy flood water is entering residential roads and homes near the "
        "central market in Ahmedabad."
    ),
):
    observed_at = observed_at or datetime.now(UTC)
    return SimpleNamespace(
        id=report_id,
        title=title,
        description=description,
        disaster_type=disaster_type,
        latitude=23.0225,
        longitude=longitude,
        location_text="Ahmedabad, Gujarat",
        created_at=observed_at,
        source_observed_at=observed_at,
        source_type=source_type,
        source_name=source_name or source_type.upper(),
        status="analyzed",
        analysis_revision=1,
        analyzed_revision=1,
    )


def test_builds_cross_source_complete_link_event_hypothesis_candidate():
    reports = [
        _report("report-a", "usgs"),
        _report("report-b", "gdacs", longitude=72.5720),
        _report("report-c", "imd", longitude=72.5708),
    ]

    clusters = build_candidate_clusters(reports)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["report_ids"] == ["report-a", "report-b", "report-c"]
    assert cluster["distinct_source_count"] == 3
    assert cluster["match_score"] >= 0.62
    assert cluster["pairwise"]


def test_does_not_call_repeated_same_source_records_multi_source():
    reports = [
        _report("report-a", "gdacs", source_name="GDACS RSS"),
        _report("report-b", "gdacs", source_name="GDACS RSS"),
    ]

    assert build_candidate_clusters(reports) == []


def test_rejects_known_disaster_type_conflicts():
    reports = [
        _report("report-a", "usgs", disaster_type="flood"),
        _report("report-b", "gdacs", disaster_type="earthquake"),
    ]

    assert build_candidate_clusters(reports) == []


def test_rejects_spatial_and_temporal_outliers():
    now = datetime.now(UTC)
    distant = [
        _report("report-a", "usgs"),
        _report("report-b", "gdacs", longitude=73.2),
    ]
    delayed = [
        _report("report-c", "usgs", observed_at=now),
        _report("report-d", "gdacs", observed_at=now + timedelta(hours=80)),
    ]

    assert build_candidate_clusters(distant) == []
    assert build_candidate_clusters(delayed) == []


def test_complete_link_prevents_transitive_chain_merging():
    reports = [
        _report("report-a", "usgs", longitude=72.5700),
        _report("report-b", "gdacs", longitude=72.6500),
        _report("report-c", "imd", longitude=72.7300),
    ]

    clusters = build_candidate_clusters(reports, max_distance_km=12.0)

    # A-B and B-C are each within 12 km; A-C is not. Complete-link must not
    # produce one three-report event merely because B bridges the two.
    assert len(clusters) == 1
    assert clusters[0]["report_ids"] == ["report-a", "report-b"]


def test_unanalyzed_or_unlocated_reports_are_not_automatically_clustered():
    pending = _report("report-a", "usgs")
    pending.analyzed_revision = 0
    unlocated = _report("report-b", "gdacs")
    unlocated.latitude = None

    assert build_candidate_clusters([pending, unlocated]) == []


def test_discovery_persists_idempotently_and_never_creates_incidents():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(
        bind=engine, expire_on_commit=False
    )
    now = datetime.now(UTC)
    with session_factory() as db:
        reports = [
            Report(
                id=str(uuid4()),
                tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
                title="Flood water rising near Ahmedabad central market",
                description=(
                    "Heavy flood water is entering residential roads and homes near "
                    "the central market in Ahmedabad."
                ),
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
                source_observed_at=now - timedelta(minutes=offset * 100),
                analysis_revision=1,
                analyzed_revision=1,
                created_at=now - timedelta(minutes=offset * 100),
            )
            for source, offset in (("usgs", 0), ("gdacs", 0.001))
        ]
        db.add_all(reports)
        db.commit()

        first = discover_event_hypotheses(db, window_hours=72, max_reports=50)
        db.commit()
        second = discover_event_hypotheses(db, window_hours=72, max_reports=50)
        db.commit()

        assert first["reports_scanned"] == 2
        assert first["hypotheses_created"] == 1
        assert first["hypothesis_snapshots_written"] == 1
        assert first["semantic"]["status"] == "disabled"
        assert second["hypotheses_created"] == 0
        assert second["hypotheses_updated"] == 1
        assert second["hypothesis_snapshots_written"] == 0
        hypotheses = db.query(EventHypothesis).all()
        assert len(hypotheses) == 1
        hypothesis = hypotheses[0]
        assert hypothesis.status == "candidate"
        assert hypothesis.scoring_version == SCORING_VERSION
        assert hypothesis.explanation["score_kind"] == "heuristic_match_score_not_probability"
        assert hypothesis.explanation["human_review_required"] is True
        assert hypothesis.explanation["automatic_incident_creation"] is False
        assert hypothesis.report_count == 2
        associations = (
            db.query(EventHypothesisReport)
            .filter_by(hypothesis_id=hypothesis.id)
            .order_by(EventHypothesisReport.report_id.asc())
            .all()
        )
        assert len(associations) == 2
        from app.db.models import EventHypothesisSnapshot
        snapshots = db.query(EventHypothesisSnapshot).filter_by(hypothesis_id=hypothesis.id).all()
        assert len(snapshots) == 1
        assert snapshots[0].change_kind == "created"
        assert not db.query(__import__("app.db.models", fromlist=["Incident"]).Incident).count()

        # A subsequent discovery must respect a human-rejected association and
        # recalculate source diversity instead of silently counting it again.
        associations[-1].association_status = "rejected"
        db.commit()
        third = discover_event_hypotheses(db, window_hours=72, max_reports=50)
        db.commit()
        db.refresh(hypothesis)
        assert third["hypotheses_updated"] == 1
        assert hypothesis.report_count == 1
        assert hypothesis.distinct_source_count == 1
        assert associations[-1].association_status == "rejected"
        snapshots = db.query(EventHypothesisSnapshot).filter_by(hypothesis_id=hypothesis.id).all()
        assert len(snapshots) == 2
    engine.dispose()


def test_human_rejection_immediately_refreshes_hypothesis_metrics():
    from sqlalchemy.orm import sessionmaker

    from app.intelligence.event_hypotheses import refresh_hypothesis_metrics_from_associations

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as db:
        reports = [
            Report(
                id=f"refresh-report-{source}",
                tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
                title="Flood water rising near Ahmedabad central market",
                description="Heavy flood water is entering roads and homes near the central market.",
                disaster_type="flood", urgency="high", status="analyzed",
                latitude=23.0225, longitude=72.5714 + offset,
                location_text="Ahmedabad, Gujarat", language="en",
                source_type=source, source_name=source.upper(), provenance="live",
                source_observed_at=now, analysis_revision=1, analyzed_revision=1,
                created_at=now,
            )
            for source, offset in (("usgs", 0.0), ("gdacs", 0.001))
        ]
        db.add_all(reports)
        hypothesis = EventHypothesis(
            id=str(uuid4()), event_key=uuid4().hex,
            anchor_report_id=reports[0].id,
            title="Flood event candidate", summary="Unverified event candidate.",
            disaster_type="flood", status="candidate",
            centroid_latitude=23.0225, centroid_longitude=72.5719,
            first_observed_at=now, last_observed_at=now,
            report_count=2, distinct_source_count=2, match_score=0.9,
            scoring_version=SCORING_VERSION,
            explanation={
                "minimum_pair_score": 0.62,
                "maximum_distance_km": 25.0,
                "maximum_time_gap_hours": 72.0,
            },
            created_at=now, updated_at=now,
        )
        db.add(hypothesis)
        db.flush()
        associations = [
            EventHypothesisReport(
                id=str(uuid4()), hypothesis_id=hypothesis.id, report_id=report.id,
                association_status="proposed", relationship_type="supports",
                pair_score=0.9, component_scores={}, reasons=[], created_at=now,
            )
            for report in reports
        ]
        db.add_all(associations)
        db.commit()

        associations[1].association_status = "rejected"
        refresh_hypothesis_metrics_from_associations(db, hypothesis)
        db.commit()
        db.refresh(hypothesis)

        assert hypothesis.report_count == 1
        assert hypothesis.distinct_source_count == 1
        assert hypothesis.match_score == 0.0
        assert hypothesis.explanation["active_association_count"] == 1
        assert hypothesis.explanation["active_report_count"] == 1
    engine.dispose()


def test_event_hypothesis_tables_have_review_and_provenance_fields():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert {"event_hypotheses", "event_hypothesis_reports", "event_discovery_runs"}.issubset(
        set(inspector.get_table_names())
    )
    hypothesis_columns = {item["name"] for item in inspector.get_columns("event_hypotheses")}
    assert {
        "event_key", "anchor_report_id", "status", "match_score", "scoring_version",
        "explanation", "reviewed_by", "reviewed_at",
    }.issubset(hypothesis_columns)
    association_columns = {item["name"] for item in inspector.get_columns("event_hypothesis_reports")}
    assert {"association_status", "pair_score", "component_scores", "reasons"}.issubset(
        association_columns
    )
    engine.dispose()



def test_event_discovery_worker_records_completed_run(monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app import worker
    from app.db.models import EventDiscoveryRun
    from app.intelligence import event_hypotheses

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    run_id = str(uuid4())
    with factory() as setup_db:
        setup_db.add(EventDiscoveryRun(
            id=run_id,
            status="queued",
            parameters={"window_hours": 72, "max_reports": 50},
            created_at=datetime.now(UTC),
        ))
        setup_db.commit()

    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(event_hypotheses, "discover_event_hypotheses", lambda *_args, **_kwargs: {
        "reports_scanned": 12,
        "clusters_found": 2,
        "hypotheses_created": 2,
        "hypotheses_updated": 0,
        "clusters_skipped": 0,
        "hypothesis_ids": ["hypothesis-a", "hypothesis-b"],
        "hypothesis_snapshots_written": 2,
        "semantic": {"status": "disabled"},
        "supervised_pair_model": {"available": False},
        "scoring_version": SCORING_VERSION,
    })

    result = worker.discover_event_hypotheses(run_id, {"window_hours": 72, "max_reports": 50})

    assert result["ok"] is True
    assert result["status"] == "completed"
    with factory() as db:
        run = db.get(EventDiscoveryRun, run_id)
        assert run.status == "completed"
        assert run.reports_scanned == 12
        assert run.hypotheses_created == 2
        assert run.result_summary["hypothesis_snapshots_written"] == 2
        assert run.result_summary["semantic"]["status"] == "disabled"
        assert run.result_summary["supervised_pair_model"]["available"] is False
        assert run.completed_at is not None
        assert run.duration_ms is not None
    engine.dispose()
