from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from app.intelligence.correlation import rank_duplicate_candidates, score_report_pair
from app.intelligence.pipeline import analyze_multimodal_report
from app.intelligence.vision import (
    ImageEvidenceError,
    analyze_image_bytes,
    extract_image_metadata,
    gps_ifd_to_coordinates,
    hash_distance,
)


def _png(color=(20, 80, 150), size=(800, 600)) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _report(**changes):
    item = {
        "id": "report-1",
        "title": "Flood water rising near central market",
        "description": "Water is entering homes and residents need assistance near the market.",
        "disaster_type": "flood",
        "latitude": 23.0225,
        "longitude": 72.5714,
        "created_at": datetime.now(UTC),
    }
    item.update(changes)
    return item


def test_gps_parser_converts_north_east_and_south_west():
    north_east = gps_ifd_to_coordinates({1: "N", 2: (23, 1, 21), 3: "E", 4: (72, 34, 17)})
    south_west = gps_ifd_to_coordinates({1: "S", 2: (23, 1, 21), 3: "W", 4: (72, 34, 17)})
    assert north_east["latitude"] > 0 and north_east["longitude"] > 0
    assert south_west["latitude"] < 0 and south_west["longitude"] < 0
    assert north_east["source"] == "image_exif_gps"


def test_gps_parser_rejects_incomplete_or_out_of_range_coordinates():
    assert gps_ifd_to_coordinates(None) is None
    assert gps_ifd_to_coordinates({1: "N", 2: (91, 0, 0), 3: "E", 4: (0, 0, 0)}) is None
    assert gps_ifd_to_coordinates({1: "Q", 2: (10, 0, 0), 3: "E", 4: (10, 0, 0)}) is None


def test_extract_image_metadata_records_dimensions_and_stripping_policy():
    metadata = extract_image_metadata(_png())
    assert metadata["width"] == 800
    assert metadata["height"] == 600
    assert metadata["format"] == "PNG"
    assert metadata["exif_stripped"] is True
    assert "exif_location" not in metadata


def test_extract_image_metadata_rejects_invalid_bytes():
    with pytest.raises(ImageEvidenceError):
        extract_image_metadata(b"not-an-image")


def test_image_analysis_returns_hash_quality_and_bounded_output():
    result = analyze_image_bytes(_png())
    assert result["available"] is True
    assert len(result["sha256"]) == 64
    assert len(result["quality"]["average_hash"]) == 16
    assert result["width"] == 800 and result["height"] == 600
    assert "damage_verification" not in result
    assert result["visual_context"]["reason"] == "disabled"


def test_image_analysis_flags_small_low_detail_images():
    result = analyze_image_bytes(_png(size=(128, 96)))
    assert "low_resolution" in result["quality_flags"]


def test_image_analysis_rejects_invalid_bytes():
    with pytest.raises(ImageEvidenceError):
        analyze_image_bytes(b"corrupt")


def test_image_analysis_rejects_oversized_pixel_dimensions(monkeypatch):
    import app.intelligence.vision as vision
    monkeypatch.setattr(vision, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageEvidenceError):
        analyze_image_bytes(_png(size=(20, 20)))


def test_average_hash_distance_is_zero_for_identical_image():
    one = analyze_image_bytes(_png())
    two = analyze_image_bytes(_png())
    assert hash_distance(one["quality"]["average_hash"], two["quality"]["average_hash"]) == 0


def test_hash_distance_rejects_malformed_hash():
    with pytest.raises(ValueError):
        hash_distance("bad", "also-bad")


def test_pipeline_text_only_is_versioned_and_advisory(monkeypatch):
    from app.intelligence import analyzer
    monkeypatch.setattr(analyzer.settings, "ai_enable_sklearn", False)
    monkeypatch.setattr(analyzer.settings, "ai_enable_pretrained_nlp", False)
    analyzer._classifier.cache_clear()
    analyzer._semantic_model.cache_clear()
    result = analyze_multimodal_report(
        "Flood in Ahmedabad", "Flood water rising and residents need urgent rescue", "gu"
    )
    assert result["pipeline_version"] == "cignal-multimodal-v1"
    assert result["advisory_only"] is True
    assert result["human_review_required"] is True
    assert result["fusion"]["category"] == "flood"
    assert result["geospatial"]["source"] == "report_text_coordinates" or result["geospatial"]["source"] == "report_text"


def test_pipeline_respects_explicit_submitted_coordinates():
    result = analyze_multimodal_report(
        "Flood", "Flood water in Ahmedabad", submitted_latitude=20.0,
        submitted_longitude=70.0, submitted_location_text="Operator supplied location",
    )
    assert result["geospatial"]["latitude"] == 20.0
    assert result["geospatial"]["source"] == "citizen_or_source_submission"


def test_pipeline_uses_exif_location_when_report_has_no_coordinates():
    gps = {"latitude": 21.1, "longitude": 72.8}
    result = analyze_multimodal_report(
        "Water incident", "Residents report a water problem",
        evidence_items=[{"id": "ev-1", "mime_type": "image/png", "content": _png(),
                         "stored_metadata": {"exif_location": gps}}],
    )
    assert result["geospatial"]["source"] == "image_exif_gps"
    assert result["geospatial"]["latitude"] == 21.1
    assert "geospatial" in result["fusion"]["modality_coverage"]


def test_pipeline_flags_conflict_between_submitted_and_exif_locations():
    result = analyze_multimodal_report(
        "Flood report", "Water is rising", submitted_latitude=23.0, submitted_longitude=72.0,
        evidence_items=[{"id": "ev-1", "mime_type": "image/png", "content": _png(),
                         "stored_metadata": {"exif_location": {"latitude": 19.0, "longitude": 73.0}}}],
    )
    assert result["geospatial"]["latitude"] == 23.0
    assert result["fusion"]["conflicts"][0]["type"] == "report_location_vs_image_exif"


def test_pipeline_skips_pdf_without_counting_it_as_a_failure():
    result = analyze_multimodal_report(
        "Report", "A non-image document is attached",
        evidence_items=[{"id": "pdf-1", "mime_type": "application/pdf", "content": b"%PDF"}],
    )
    assert result["vision"]["non_image_evidence_skipped"] == 1
    assert result["vision"]["images_failed"] == 0


def test_pipeline_records_corrupt_image_without_failing_whole_report():
    result = analyze_multimodal_report(
        "Fire report", "Smoke is visible near the road",
        evidence_items=[{"id": "broken", "mime_type": "image/jpeg", "content": b"broken"}],
    )
    assert result["vision"]["images_failed"] == 1
    assert result["vision"]["images"][0]["available"] is False


def test_pipeline_detects_identical_images_as_candidates_only():
    content = _png()
    result = analyze_multimodal_report(
        "Fire report", "Smoke is visible near the road",
        evidence_items=[
            {"id": "one", "mime_type": "image/png", "content": content},
            {"id": "two", "mime_type": "image/png", "content": content},
        ],
    )
    assert len(result["vision"]["near_duplicate_image_candidates"]) == 1
    assert result["vision"]["near_duplicate_image_candidates"][0]["candidate_only"] is True


def test_pipeline_rejects_unpaired_submitted_coordinates():
    with pytest.raises(ValueError):
        analyze_multimodal_report("Flood", "Water rising", submitted_latitude=23.0)


def test_correlation_scores_nearby_same_type_reports_higher():
    now = datetime.now(UTC)
    current = _report(created_at=now)
    nearby = _report(id="report-2", created_at=now - timedelta(hours=2))
    distant = _report(id="report-3", created_at=now - timedelta(days=60), latitude=40.7, longitude=-74.0)
    assert score_report_pair(current, nearby)["score"] > score_report_pair(current, distant)["score"]


def test_correlation_penalizes_incompatible_disaster_types():
    same = score_report_pair(_report(), _report(id="same", disaster_type="flood"))
    different = score_report_pair(_report(), _report(id="different", disaster_type="earthquake"))
    assert same["score"] > different["score"]


def test_correlation_rank_excludes_self_and_orders_candidates():
    current = _report()
    candidates = [
        _report(),
        _report(id="far", title="Different earthquake elsewhere", description="Strong ground shaking.",
                disaster_type="earthquake", latitude=40.0, longitude=-70.0),
        _report(id="near", created_at=datetime.now(UTC) - timedelta(hours=1)),
    ]
    result = rank_duplicate_candidates(current, candidates, minimum_score=0.0)
    assert all(row["candidate_id"] != current["id"] for row in result)
    assert result[0]["candidate_id"] == "near"


def test_correlation_auto_pointer_requires_very_strong_match():
    current = _report()
    exact = _report(id="exact", created_at=current["created_at"])
    weak = _report(id="weak", title="Weather update", description="General weather information.")
    assert score_report_pair(current, exact)["automatic_duplicate_pointer_eligible"] is True
    assert score_report_pair(current, weak)["automatic_duplicate_pointer_eligible"] is False


def test_correlation_rejects_invalid_limits():
    with pytest.raises(ValueError):
        rank_duplicate_candidates(_report(), [], limit=0)
    with pytest.raises(ValueError):
        rank_duplicate_candidates(_report(), [], minimum_score=1.2)


def test_multilingual_category_classifier_holdout(monkeypatch):
    from app.core.config import settings
    from app.intelligence import analyzer

    monkeypatch.setattr(settings, "ai_enable_sklearn", True)
    monkeypatch.setattr(settings, "ai_enable_pretrained_nlp", False)
    analyzer._classifier.cache_clear()
    analyzer._semantic_model.cache_clear()
    holdout = [
        ("flood", "River water is overflowing into houses after heavy rainfall."),
        ("flood", "પૂરના પાણીમાં રસ્તો ડૂબી ગયો છે અને ઘરોમાં પાણી ઘૂસ્યું છે."),
        ("flood", "बाढ़ के कारण सड़क और घर जलमग्न हैं।"),
        ("earthquake", "A strong tremor shook buildings and cracked walls."),
        ("earthquake", "ભૂકંપના આંચકાથી મકાનમાં તિરાડ પડી."),
        ("earthquake", "भूकंप के झटकों से इमारतें हिल रही हैं।"),
        ("fire", "Flames and smoke are spreading from the warehouse."),
        ("fire", "જંગલમાં આગ લાગી છે અને ધુમાડો ફેલાયો છે."),
        ("fire", "दुकान में आग लगी है और धुआं निकल रहा है।"),
        ("cyclone", "Cyclonic winds and storm surge are affecting the coast."),
        ("cyclone", "દરિયાકાંઠે વાવાઝોડાના ભારે પવન ફૂંકાઈ રહ્યા છે."),
        ("cyclone", "चक्रवात के कारण समुद्र में ऊंची लहरें उठ रही हैं।"),
        ("landslide", "Rocks and mud have blocked the mountain road."),
        ("landslide", "પર્વત પરથી કાટમાળ ધસી પડ્યો અને રસ્તો બંધ થયો."),
        ("landslide", "पहाड़ी से मलबा गिरा और सड़क बंद हो गई।"),
        ("other", "Residents need drinking water and medical supplies."),
        ("other", "રાહત કેમ્પમાં દવા અને પીવાના પાણીની જરૂર છે."),
        ("other", "लोगों को भोजन, आश्रय और दवाइयों की जरूरत है।"),
    ]
    try:
        predictions = [analyzer.classify_category(text)[0] for _, text in holdout]
        accuracy = sum(pred == expected for pred, (expected, _) in zip(predictions, holdout, strict=True)) / len(holdout)
        assert accuracy >= 0.90
    finally:
        analyzer._classifier.cache_clear()
        analyzer._semantic_model.cache_clear()


def test_report_analysis_is_worker_persisted(monkeypatch):
    import sys
    import types
    from io import BytesIO
    from uuid import uuid4

    from PIL import Image
    if "rq" not in sys.modules:
        rq_module = types.ModuleType("rq")
        rq_module.Queue = object
        rq_module.Worker = object
        sys.modules["rq"] = rq_module

    from app import worker
    from app.db.models import Evidence, Report
    from app.db.session import SessionLocal

    output = BytesIO()
    Image.new("RGB", (800, 600), (30, 120, 180)).save(output, format="PNG")
    content = output.getvalue()
    stored = {}

    class MemoryStorage:
        def read(self, key):
            return stored[key]

    monkeypatch.setattr(worker, "get_storage", lambda: MemoryStorage())
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)
    report_id = str(uuid4())
    evidence_id = str(uuid4())
    evidence_key = f"{report_id}/image.png"
    stored[evidence_key] = content
    with SessionLocal() as db:
        report = Report(
            id=report_id, tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
            title="Flood water near the river", description="Water is rising near the houses and residents need help.",
            disaster_type="unknown", urgency="unknown", status="submitted", language="en",
            source_type="citizen", source_name="Test", provenance="submitted",
        )
        evidence = Evidence(
            id=evidence_id, report_id=report_id, original_filename="field.png", stored_key=evidence_key,
            mime_type="image/png", sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), image_metadata={"width": 800},
        )
        db.add(report)
        db.commit()
        db.add(evidence)
        db.commit()

    result = worker.process_report(report_id)
    assert result["ok"] is True
    assert result["analysis"]["pipeline_version"] == "cignal-multimodal-v1"
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        evidence = db.get(Evidence, evidence_id)
        assert report.status == "analyzed"
        assert report.ai_analysis["vision"]["images_analyzed"] == 1
        assert evidence.image_metadata["cignal_vision"]["available"] is True
        db.delete(evidence)
        db.delete(report)
        db.commit()


def test_pipeline_refuses_to_analyze_evidence_with_hash_mismatch():
    content = _png()
    result = analyze_multimodal_report(
        "Flood report", "Water is rising",
        evidence_items=[{"id": "tampered", "mime_type": "image/png", "content": content,
                         "expected_sha256": "0" * 64, "expected_size": len(content)}],
    )
    assert result["vision"]["images_analyzed"] == 0
    assert result["vision"]["images_failed"] == 1
    assert result["vision"]["images"][0]["reason"] == "integrity_hash_mismatch"


def test_pipeline_refuses_to_analyze_evidence_with_size_mismatch():
    content = _png()
    result = analyze_multimodal_report(
        "Flood report", "Water is rising",
        evidence_items=[{"id": "truncated", "mime_type": "image/png", "content": content,
                         "expected_sha256": __import__("hashlib").sha256(content).hexdigest(),
                         "expected_size": len(content) + 1}],
    )
    assert result["vision"]["images_analyzed"] == 0
    assert result["vision"]["images"][0]["reason"] == "integrity_size_mismatch"


def test_pipeline_enforces_per_report_image_limit(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ai_max_images_per_report", 1)
    content = _png()
    result = analyze_multimodal_report(
        "Flood report", "Water is rising",
        evidence_items=[
            {"id": "one", "mime_type": "image/png", "content": content},
            {"id": "two", "mime_type": "image/png", "content": content},
        ],
    )
    assert result["vision"]["images_analyzed"] == 1
    assert result["vision"]["images_skipped_limit"] == 1
    assert result["vision"]["max_images_per_report"] == 1


def test_stale_analysis_job_is_superseded_and_latest_revision_is_queued(monkeypatch):
    import sys
    import types
    from uuid import uuid4

    if "redis" not in sys.modules:
        redis_module = types.ModuleType("redis")
        redis_module.Redis = object
        exceptions_module = types.ModuleType("redis.exceptions")
        exceptions_module.RedisError = Exception
        redis_module.exceptions = exceptions_module
        sys.modules["redis"] = redis_module
        sys.modules["redis.exceptions"] = exceptions_module
    if "rq" not in sys.modules:
        rq_module = types.ModuleType("rq")
        rq_module.Queue = object
        rq_module.Worker = object
        sys.modules["rq"] = rq_module

    from app import worker
    from app.db.models import Report
    from app.db.session import SessionLocal, initialize_database

    initialize_database()
    report_id = str(uuid4())
    with SessionLocal() as db:
        db.add(Report(
            id=report_id, tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
            title="Flood update from local residents", description="Water is entering several residential lanes and the road is blocked.",
            disaster_type="flood", urgency="high", status="submitted", language="en",
            source_type="citizen", source_name="Test", provenance="submitted",
            analysis_revision=2, analyzed_revision=1,
        ))
        db.commit()

    result = worker.process_report(report_id, 1)

    assert result["status"] == "superseded"
    assert result["superseding_job_queued"] is True
    assert result["superseding_job_outboxed"] is True
    with SessionLocal() as db:
        from app.db.models import BackgroundJobOutbox
        outbox = db.query(BackgroundJobOutbox).filter_by(
            job_id=f"cignal-report-analysis-{report_id}-r2"
        ).one()
        assert outbox.status == "pending"
        assert outbox.task_args == [report_id, 2]
        report = db.get(Report, report_id)
        assert report.analysis_revision == 2
        assert report.analyzed_revision == 1
        assert report.ai_analysis is None
        db.delete(report)
        db.commit()


def test_analysis_revision_change_during_compute_discards_stale_result(monkeypatch):
    import sys
    import types
    from uuid import uuid4

    if "redis" not in sys.modules:
        redis_module = types.ModuleType("redis")
        redis_module.Redis = object
        exceptions_module = types.ModuleType("redis.exceptions")
        exceptions_module.RedisError = Exception
        redis_module.exceptions = exceptions_module
        sys.modules["redis"] = redis_module
        sys.modules["redis.exceptions"] = exceptions_module
    if "rq" not in sys.modules:
        rq_module = types.ModuleType("rq")
        rq_module.Queue = object
        rq_module.Worker = object
        sys.modules["rq"] = rq_module

    from app import worker
    from app.db.models import Report
    from app.db.session import SessionLocal, initialize_database

    initialize_database()
    report_id = str(uuid4())
    with SessionLocal() as db:
        db.add(Report(
            id=report_id, tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
            title="Flood report during processing", description="Residents report rising water around the primary school.",
            disaster_type="flood", urgency="high", status="submitted", language="en",
            source_type="citizen", source_name="Test", provenance="submitted",
            analysis_revision=1, analyzed_revision=0,
        ))
        db.commit()

    def concurrent_update(*_args, **_kwargs):
        with SessionLocal() as concurrent_db:
            current = concurrent_db.get(Report, report_id)
            current.analysis_revision = 2
            concurrent_db.commit()
        return {
            "text": {"category": "flood", "urgency": "high", "confidence": 0.8},
            "summary": "Advisory test analysis", "geospatial": {"latitude": None, "longitude": None, "location_text": None},
            "vision": {"images": []}, "pipeline_version": "test-pipeline",
        }

    monkeypatch.setattr(worker, "analyze_multimodal_report", concurrent_update)
    monkeypatch.setattr(worker, "get_storage", lambda: object())
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)

    result = worker.process_report(report_id, 1)

    assert result["status"] == "superseded"
    assert result["superseding_job_outboxed"] is True
    with SessionLocal() as db:
        from app.db.models import BackgroundJobOutbox
        outbox = db.query(BackgroundJobOutbox).filter_by(
            job_id=f"cignal-report-analysis-{report_id}-r2"
        ).one()
        assert outbox.status == "pending"
        db.delete(outbox)
        report = db.get(Report, report_id)
        assert report.analysis_revision == 2
        assert report.analyzed_revision == 0
        assert report.ai_analysis is None
        db.delete(report)
        db.commit()
