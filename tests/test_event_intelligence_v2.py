from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    EventHypothesis,
    EventHypothesisReport,
    IntegrationRun,
    Report,
    ReportSemanticEmbedding,
)


def _report(source: str, text: str, *, report_id: str | None = None):
    now = datetime.now(UTC)
    return Report(
        id=report_id or str(uuid4()),
        tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
        title=text[:100],
        description=text,
        disaster_type="flood",
        urgency="high",
        status="analyzed",
        latitude=23.0225,
        longitude=72.5714,
        location_text="Ahmedabad, Gujarat",
        language="en",
        source_type=source,
        source_name=source.upper(),
        provenance="live",
        source_observed_at=now,
        analysis_revision=1,
        analyzed_revision=1,
        created_at=now,
    )


def test_semantic_embedding_cache_is_content_addressed_and_reused(monkeypatch):
    from app.intelligence import semantic

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    class FakeModel:
        def __init__(self):
            self.calls = []

        def encode(self, texts, **_kwargs):
            self.calls.append(list(texts))
            result = []
            for text in texts:
                token = "flood" in text.casefold()
                result.append([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] if token else [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            return result

    model = FakeModel()
    monkeypatch.setattr(semantic, "_load_model", lambda: (model, None))
    with factory() as db:
        first = _report("usgs", "Flood water entered homes after rain")
        second = _report("gdacs", "પૂરનું પાણી ઘરોમાં પ્રવેશ્યું ભારે વરસાદ પછી")
        db.add_all([first, second])
        db.commit()

        initial = semantic.embed_reports(db, [first, second])
        assert initial.status == "available"
        assert initial.encoded_count == 2
        assert initial.cache_hits == 0
        assert initial.similarities[tuple(sorted((first.id, second.id)))] == pytest.approx(0.0)
        db.commit()

        cached = semantic.embed_reports(db, [first, second])
        assert cached.cache_hits == 2
        assert cached.encoded_count == 0
        assert len(model.calls) == 1

        first.description = "Flood water entered many homes after heavy rain"
        refreshed = semantic.embed_reports(db, [first, second])
        assert refreshed.encoded_count == 1
        assert refreshed.cache_hits == 1
        assert db.query(ReportSemanticEmbedding).count() == 2
    engine.dispose()


def test_semantic_provider_fails_closed_when_feature_disabled(monkeypatch):
    from app.core.config import settings
    from app.intelligence.semantic import embed_reports

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "ai_enable_pretrained_nlp", False)
    with factory() as db:
        result = embed_reports(db, [_report("usgs", "Flood water reported"), _report("gdacs", "Water rising")])
        assert result.status == "disabled"
        assert result.similarities == {}
    engine.dispose()


def test_pretrained_semantic_model_never_attempts_an_implicit_download(monkeypatch):
    import sys
    from types import ModuleType

    from app.core.config import settings
    from app.intelligence import analyzer

    calls = {}

    class FakeSentenceTransformer:
        def __init__(self, model_id, **kwargs):
            calls["model_id"] = model_id
            calls.update(kwargs)

        def encode(self, texts, **_kwargs):
            return [[1.0, 0.0] for _ in texts]

    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setattr(settings, "ai_enable_pretrained_nlp", True)
    analyzer._semantic_model.cache_clear()
    try:
        result = analyzer._semantic_model()
        assert result is not None
        assert calls["local_files_only"] is True
        assert calls["device"] == "cpu"
    finally:
        analyzer._semantic_model.cache_clear()


def test_semantic_similarity_changes_pair_score_but_remains_a_score_not_probability():
    from app.intelligence.event_hypotheses import _pair_result

    first = _report("usgs", "Flood water entered homes near the river")
    second = _report("gdacs", "Rising water inundated nearby residential buildings")
    baseline = _pair_result(
        first, second, minimum_pair_score=0.0, max_distance_km=25,
        max_time_gap_hours=72,
    )
    semantic = _pair_result(
        first, second, minimum_pair_score=0.0, max_distance_km=25,
        max_time_gap_hours=72, semantic_similarity=0.91,
    )
    assert semantic["score"] > baseline["score"]
    assert semantic["semantic_similarity"] == pytest.approx(0.91)
    assert "strong_multilingual_semantic_alignment" in semantic["reasons"]


def test_source_profile_separates_ingestion_health_from_analyst_acceptance():
    from app.db.models import SourceReliabilityProfile
    from app.services.source_reliability import (
        rebuild_source_reliability_profiles,
        source_reliability_dict,
        wilson_lower_bound,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as db:
        reports = [_report("usgs", f"Flood report {index}") for index in range(2)]
        db.add_all(reports)
        hyp = EventHypothesis(
            id=str(uuid4()), event_key=uuid4().hex, anchor_report_id=reports[0].id,
            title="Candidate", summary="Unverified", disaster_type="flood", status="candidate",
            centroid_latitude=23.02, centroid_longitude=72.57,
            first_observed_at=now, last_observed_at=now, report_count=2,
            distinct_source_count=1, match_score=0.7, scoring_version="test-v1",
            explanation={}, created_at=now, updated_at=now,
        )
        db.add(hyp)
        db.flush()
        db.add_all([
            EventHypothesisReport(
                id=str(uuid4()), hypothesis_id=hyp.id, report_id=reports[0].id,
                association_status="confirmed", relationship_type="supports",
                pair_score=0.8, component_scores={}, reasons=[], reviewed_at=now,
            ),
            EventHypothesisReport(
                id=str(uuid4()), hypothesis_id=hyp.id, report_id=reports[1].id,
                association_status="rejected", relationship_type="supports",
                pair_score=0.3, component_scores={}, reasons=[], reviewed_at=now,
            ),
        ])
        for status in ("success", "partial", "failed"):
            db.add(IntegrationRun(
                id=str(uuid4()), source_type="usgs", source_name="USGS",
                trigger="scheduled", status=status,
                records_fetched=10, records_inserted=5, records_rejected=1,
                started_at=now, completed_at=now,
            ))
        db.commit()

        summary = rebuild_source_reliability_profiles(db)
        db.commit()
        assert summary["profiles_refreshed"] == 1
        profile = db.get(SourceReliabilityProfile, "usgs:usgs")
        assert profile is not None
        assert profile.ingestion_runs == 3
        assert profile.operational_success_rate == pytest.approx(1 / 3, abs=0.001)
        assert profile.reviewed_associations == 2
        assert profile.confirmed_associations == 1
        assert profile.rejected_associations == 1
        assert profile.review_acceptance_posterior_mean == pytest.approx(0.5)
        assert profile.review_confidence == "insufficient"
        output = source_reliability_dict(profile)
        assert "Neither metric proves source truthfulness" in output["interpretation"]
        assert wilson_lower_bound(0, 0) is None
    engine.dispose()


def test_event_pair_training_uses_disjoint_groups_and_exports_model(tmp_path):
    import joblib

    from app.intelligence.event_pair_training import train_event_pair_model

    fixture = Path(__file__).parent / "fixtures" / "event_pairs_synthetic.jsonl"
    output = tmp_path / "event-pair-smoke.joblib"
    result = train_event_pair_model(fixture, output, random_state=11)
    assert output.is_file()
    assert result["training_samples"] == 16
    holdout = result["holdout"]
    assert set(holdout["train_groups"]).isdisjoint(set(holdout["test_groups"]))
    assert holdout["metrics"]["score_interpretation"] == "uncalibrated_classifier_score_not_probability"
    assert "heuristic_baseline_metrics" in holdout
    assert "comparison_vs_heuristic" in holdout
    baseline_matrix = holdout["heuristic_baseline_metrics"]["confusion_matrix_tn_fp_fn_tp"]
    assert len(baseline_matrix) == 2 and all(len(row) == 2 for row in baseline_matrix)
    artifact = joblib.load(output)
    assert artifact["schema"] == "cignal-event-pair-model-v1"
    assert len(artifact["feature_names"]) == 8


def test_new_event_pair_artifact_becomes_visible_without_worker_restart(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.intelligence import event_pair_model
    from app.intelligence.event_pair_training import train_event_pair_model

    fixture = Path(__file__).parent / "fixtures" / "event_pairs_synthetic.jsonl"
    artifact = tmp_path / "event-pair-live.joblib"
    monkeypatch.setattr(settings, "ai_enable_sklearn", True)
    monkeypatch.setattr(settings, "event_pair_model_path", artifact)
    event_pair_model._load_artifact_cached.cache_clear()
    try:
        assert event_pair_model._load_artifact() is None
        train_event_pair_model(fixture, artifact, random_state=17)
        loaded = event_pair_model._load_artifact()
        assert loaded is not None
        assert loaded["schema"] == event_pair_model.ARTIFACT_SCHEMA
    finally:
        event_pair_model._load_artifact_cached.cache_clear()


def test_discovery_report_cap_prioritizes_newest_observations():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.intelligence.event_hypotheses import discover_event_hypotheses

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as db:
        older = _report("india", "Older unrelated flood observation")
        older.source_observed_at = now - timedelta(hours=60)
        older.created_at = older.source_observed_at
        recent_a = _report("usgs", "River flood water entered homes near Ahmedabad")
        recent_a.source_observed_at = now - timedelta(minutes=20)
        recent_a.created_at = recent_a.source_observed_at
        recent_b = _report("gdacs", "Flood water inundated residential homes around Ahmedabad")
        recent_b.source_observed_at = now - timedelta(minutes=10)
        recent_b.created_at = recent_b.source_observed_at
        db.add_all([older, recent_a, recent_b])
        db.commit()

        result = discover_event_hypotheses(db, window_hours=72, max_reports=2)
        db.commit()

        assert result["reports_scanned"] == 2
        assert result["hypotheses_created"] == 1
        hypothesis = db.query(EventHypothesis).one()
        linked_ids = {
            row.report_id
            for row in db.query(EventHypothesisReport).filter_by(hypothesis_id=hypothesis.id)
        }
        assert linked_ids == {recent_a.id, recent_b.id}
        assert older.id not in linked_ids
    engine.dispose()


def test_supervised_pair_model_scoring_is_explicitly_non_probabilistic(monkeypatch):
    import numpy as np
    from scipy.sparse import csr_matrix

    from app.intelligence import event_pair_model

    class FakeVectorizer:
        def transform(self, texts):
            return csr_matrix([[1.0, 0.0], [0.9, 0.1]])

    class FakeClassifier:
        classes_ = [0, 1]

        def predict_proba(self, features):
            return np.asarray([[0.2, 0.8]])

    monkeypatch.setattr(event_pair_model, "_load_artifact", lambda: {
        "schema": event_pair_model.ARTIFACT_SCHEMA,
        "feature_names": list(event_pair_model.FEATURE_NAMES),
        "vectorizer": FakeVectorizer(),
        "classifier": FakeClassifier(),
        "model_version": "unit-test-model",
    })
    result = event_pair_model.score_event_pair_model(
        _report("usgs", "Flood entered residential homes"),
        _report("gdacs", "Rising flood water entered houses"),
    )
    assert result["score"] == pytest.approx(0.8)
    assert result["model_version"] == "unit-test-model"
    assert len(result["feature_names"]) == 8


def test_event_pair_loader_rejects_exact_pair_across_split_groups(tmp_path):
    from app.intelligence.event_pair_training import (
        EventPairDatasetError,
        load_event_pair_jsonl,
    )

    rows = []
    for index in range(6):
        rows.append({
            "split_group": f"event-{index}",
            "text_a": f"Unique disaster observation alpha {index}",
            "text_b": f"Unique disaster observation beta {index}",
            "same_event": index % 2,
        })
    rows.append({
        "split_group": "different-event-group",
        "text_a": rows[0]["text_a"],
        "text_b": rows[0]["text_b"],
        "same_event": rows[0]["same_event"],
    })
    dataset = tmp_path / "leaked-pairs.jsonl"
    dataset.write_text("\n".join(json.dumps(row) for row in rows))

    with pytest.raises(EventPairDatasetError, match="exact-pair leakage"):
        load_event_pair_jsonl(dataset)


def test_event_pair_evaluator_rejects_training_group_overlap():
    from app.intelligence.event_pair_training import (
        EventPairDatasetError,
        validate_external_evaluation_groups,
    )

    payload = {"training_split_groups": ["event-a", "event-b"]}
    rows = [
        {"split_group": "event-b", "same_event": 1},
        {"split_group": "event-c", "same_event": 0},
    ]

    with pytest.raises(EventPairDatasetError, match="overlap model training groups"):
        validate_external_evaluation_groups(payload, rows)
