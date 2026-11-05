from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api.schemas import AcquisitionBackfillRequest, PairCandidateMiningRequest


def test_acquisition_intelligence_routes_are_registered_with_expected_methods():
    from app.api.main import app

    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    expected = {
        ("/api/integrations/backfills", "POST"),
        ("/api/integrations/backfills", "GET"),
        ("/api/integrations/backfills/{run_id}", "GET"),
        ("/api/integrations/backfills/{run_id}/resume", "POST"),
        ("/api/integrations/observations", "GET"),
        ("/api/integrations/observations/{observation_id}/replay", "POST"),
        ("/api/research/datasets/{dataset_id}/pair-candidates/mine", "POST"),
        ("/api/research/datasets/{dataset_id}/pair-candidates", "GET"),
        ("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}", "GET"),
        ("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}/resume", "POST"),
        ("/api/research/datasets/{dataset_id}/pair-candidates/{candidate_id}/dismiss", "POST"),
        ("/api/research/model-evaluations", "GET"),
    }
    assert expected <= routes


def test_backfill_request_accepts_timezone_aware_bounded_window():
    now = datetime.now(UTC)
    request = AcquisitionBackfillRequest(
        source_type="usgs", start_at=now - timedelta(days=10), end_at=now,
        window_days=2, max_records=500,
    )
    assert request.source_type == "usgs"
    assert request.window_days == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"source_type": "unknown", "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-01-02T00:00:00Z"},
        {"source_type": "usgs", "start_at": "2026-01-02T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"},
        {"source_type": "usgs", "start_at": "2025-01-01T00:00:00Z", "end_at": "2026-01-03T00:00:00Z"},
        {"source_type": "nasa_eonet", "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-01-15T00:00:00Z", "window_days": 15},
    ],
)
def test_backfill_request_rejects_unsupported_or_unbounded_scope(payload):
    with pytest.raises(ValidationError):
        AcquisitionBackfillRequest.model_validate(payload)


def test_pair_candidate_mining_request_is_bounded():
    with pytest.raises(ValidationError):
        PairCandidateMiningRequest(max_reports=100_000)
    with pytest.raises(ValidationError):
        PairCandidateMiningRequest(max_pairs_scored=1_000_000)
