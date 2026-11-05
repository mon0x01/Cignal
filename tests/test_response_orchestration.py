from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    Incident,
    IncidentAction,
    IncidentActionDependency,
    IncidentResponsePlan,
)
from app.services.response_orchestration import dependency_blockers, refresh_response_plan_status
from app.services.response_playbooks import get_playbook, list_playbooks, validate_dependency_graph


def test_catalog_exposes_only_supported_versioned_coordination_templates():
    templates = list_playbooks()
    assert {item["disaster_type"] for item in templates} == {
        "flood", "earthquake", "fire", "cyclone", "landslide"
    }
    for template in templates:
        assert template["template_version"] == 1
        assert template["policy"]["human_review_required"] is True
        assert template["policy"]["external_side_effects"] is False
        assert len(template["steps"]) == 5
        assert validate_dependency_graph(template["steps"])
        warning = next(step for step in template["steps"] if step["action_type"] == "public_warning")
        assert warning["depends_on"]
    assert get_playbook("unknown_coordination_v1") is None


def test_playbook_graph_validator_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(ValueError, match="cycle"):
        validate_dependency_graph([
            {"step_key": "a", "depends_on": ["b"]},
            {"step_key": "b", "depends_on": ["a"]},
        ])
    with pytest.raises(ValueError, match="Invalid dependency"):
        validate_dependency_graph([{"step_key": "a", "depends_on": ["missing"]}])


def test_dependency_blockers_and_plan_state_follow_task_transitions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    incident_id, plan_id = str(uuid4()), str(uuid4())
    first_id, second_id = str(uuid4()), str(uuid4())
    with Session(engine) as db:
        db.add(Incident(
            id=incident_id, incident_code="CIG-ORCH-TEST", title="Coordination test incident",
            summary="Synthetic test incident only.", disaster_type="flood", severity="high",
            status="verified", source_count=1, confidence=0.9, provenance="synthetic",
        ))
        plan = IncidentResponsePlan(
            id=plan_id, incident_id=incident_id, template_key="flood_coordination_v1",
            template_version=1, title="Flood plan", status="active", idempotency_key=str(uuid4()),
            template_snapshot={"steps": []}, created_at=now, updated_at=now,
        )
        db.add(plan)
        first = IncidentAction(
            id=first_id, incident_id=incident_id, response_plan_id=plan_id, sequence_order=0,
            title="First task", description="First task description", action_type="monitoring",
            priority="high", status="proposed", approval_required=False,
            approval_status="not_required", version=1, created_at=now, updated_at=now,
        )
        second = IncidentAction(
            id=second_id, incident_id=incident_id, response_plan_id=plan_id, sequence_order=1,
            title="Second task", description="Second task description", action_type="logistics",
            priority="normal", status="approved", approval_required=False,
            approval_status="not_required", version=1, created_at=now, updated_at=now,
        )
        db.add_all([first, second])
        db.flush()
        db.add(IncidentActionDependency(
            id=str(uuid4()), plan_id=plan_id, action_id=second_id,
            depends_on_action_id=first_id, created_at=now,
        ))
        db.flush()
        blockers = dependency_blockers(db, plan_id, second_id)
        assert blockers == [{"action_id": first_id, "title": "First task", "status": "proposed"}]
        first.status = "completed"
        assert dependency_blockers(db, plan_id, second_id) == []
        first.status = "completed"
        second.status = "completed"
        refreshed = refresh_response_plan_status(db, plan_id)
        assert refreshed is not None and refreshed.status == "completed"
        db.commit()
    engine.dispose()
