from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models import (
    EventPairCandidate,
    EventPairLabel,
    PairCandidateMiningRun,
    Report,
    ResearchDataset,
)
from app.db.session import SessionLocal, initialize_database
from app.services.active_learning import mine_event_pair_candidates


def test_candidate_mining_creates_review_only_candidates_and_never_labels_them():
    initialize_database()
    with SessionLocal() as db:
        db.query(EventPairCandidate).delete()
        db.query(PairCandidateMiningRun).delete()
        db.query(EventPairLabel).delete()
        db.query(Report).filter(Report.source_record_id.like("qa-active-%")).delete(synchronize_session=False)
        db.query(ResearchDataset).filter_by(slug="qa-active-learning").delete()
        db.commit()

        dataset = ResearchDataset(
            id=str(uuid4()), slug="qa-active-learning", name="QA Active Learning",
            description="Test-only candidate mining dataset", task_type="event_pair_classification",
            purpose="Validate unlabelled candidate generation", license_identifier="CC0-1.0",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            legal_basis="public_open_data", consent_scope="public_sources",
            source_types=["usgs", "gdacs"], coordinate_precision=2,
            privacy_policy="exclude_direct_identifiers", status="active",
        )
        db.add(dataset)
        db.flush()
        now = datetime.now(UTC)
        reports = []
        for index in range(8):
            source = "usgs" if index % 2 == 0 else "gdacs"
            reports.append(Report(
                id=str(uuid4()), tracking_code=f"QA-{index:08d}", title=f"Flood response update sector {index}",
                description=f"Flood water reported near the eastern river embankment, update number {index}. Roads and houses are affected in this district.",
                disaster_type="flood", urgency="unknown", status="submitted",
                latitude=23.0 + (index * 0.01), longitude=72.5 + (index * 0.01),
                location_text="Test region", language="en", source_type=source, source_name=source,
                source_record_id=f"qa-active-{index}", source_url=None, provenance="historical",
                source_observed_at=now + timedelta(minutes=index * 10), source_metadata={},
            ))
        db.add_all(reports)
        db.flush()
        run = PairCandidateMiningRun(
            id=str(uuid4()), dataset_id=dataset.id, status="running",
            parameters={"max_reports": 100, "window_hours": 72, "max_distance_km": 50,
                        "max_pairs_scored": 10000, "top_candidates": 100},
        )
        db.add(run)
        db.commit()

        result = mine_event_pair_candidates(db, run_id=run.id)
        assert result["ok"] is True
        assert result["candidates_created"] > 0
        candidates = db.query(EventPairCandidate).filter_by(dataset_id=dataset.id).all()
        assert candidates
        assert all(candidate.status == "pending" for candidate in candidates)
        assert db.query(EventPairLabel).filter_by(dataset_id=dataset.id).count() == 0
        assert run.status == "completed"


def test_candidate_mining_surfaces_geographically_conflicting_text_matches_as_hard_negatives():
    initialize_database()
    with SessionLocal() as db:
        db.query(EventPairCandidate).delete()
        db.query(PairCandidateMiningRun).delete()
        db.query(EventPairLabel).delete()
        db.query(Report).filter(Report.source_record_id.like("qa-hard-negative-%")).delete(synchronize_session=False)
        db.query(ResearchDataset).filter_by(slug="qa-hard-negative").delete()
        db.commit()

        dataset = ResearchDataset(
            id=str(uuid4()), slug="qa-hard-negative", name="QA Hard Negative Dataset",
            description="Test geographically conflicting near-match selection", task_type="event_pair_classification",
            purpose="Validate hard-negative candidate hints", license_identifier="CC0-1.0",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            legal_basis="public_open_data", consent_scope="public_sources", source_types=["qa_hard_negative"],
            coordinate_precision=2, privacy_policy="exclude_direct_identifiers", status="active",
        )
        db.add(dataset)
        db.flush()
        when = datetime.now(UTC)
        reports = [
            Report(
                id=str(uuid4()), tracking_code="QA-HN-00000001",
                title="Major flood emergency at the eastern river embankment",
                description="Flood water inundates roads and houses near the river embankment. Emergency response teams are assessing the situation.",
                disaster_type="flood", urgency="unknown", status="submitted",
                latitude=23.0, longitude=72.5, location_text="Region A", language="en",
                source_type="qa_hard_negative", source_name="test", source_record_id="qa-hard-negative-a",
                source_url=None, provenance="historical", source_observed_at=when, source_metadata={},
            ),
            Report(
                id=str(uuid4()), tracking_code="QA-HN-00000002",
                title="Major flood emergency at the eastern river embankment",
                description="Flood water inundates roads and houses near the river embankment. Emergency response teams are assessing the situation.",
                disaster_type="flood", urgency="unknown", status="submitted",
                latitude=40.7, longitude=-74.0, location_text="Region B", language="en",
                source_type="qa_hard_negative", source_name="test", source_record_id="qa-hard-negative-b",
                source_url=None, provenance="historical", source_observed_at=when, source_metadata={},
            ),
        ]
        db.add_all(reports)
        db.flush()
        run = PairCandidateMiningRun(
            id=str(uuid4()), dataset_id=dataset.id, status="running",
            parameters={"max_reports": 100, "window_hours": 24, "max_distance_km": 100,
                        "max_pairs_scored": 1000, "top_candidates": 10},
        )
        db.add(run)
        db.commit()

        result = mine_event_pair_candidates(db, run_id=run.id)
        assert result["candidates_created"] == 1
        candidate = db.query(EventPairCandidate).filter_by(dataset_id=dataset.id).one()
        assert candidate.status == "pending"
        assert candidate.candidate_reasons["hard_negative_hint"] is True
        assert candidate.candidate_reasons["distance_km"] > 100
