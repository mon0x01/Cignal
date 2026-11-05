"""Dependency and plan-state helpers for incident response coordination."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import IncidentAction, IncidentActionDependency, IncidentResponsePlan, utcnow


def dependency_blockers(db: Session, plan_id: str, action_id: str) -> list[dict]:
    edges = (
        db.query(IncidentActionDependency)
        .filter_by(plan_id=plan_id, action_id=action_id)
        .order_by(IncidentActionDependency.depends_on_action_id.asc())
        .all()
    )
    blockers = []
    for edge in edges:
        prerequisite = db.get(IncidentAction, edge.depends_on_action_id)
        if prerequisite is None or prerequisite.status != "completed":
            blockers.append({
                "action_id": edge.depends_on_action_id,
                "title": prerequisite.title if prerequisite else "Missing prerequisite",
                "status": prerequisite.status if prerequisite else "missing",
            })
    return blockers


def refresh_response_plan_status(db: Session, plan_id: str) -> IncidentResponsePlan | None:
    plan = db.query(IncidentResponsePlan).filter_by(id=plan_id).with_for_update().first()
    if plan is None or plan.status in {"cancelled", "completed"}:
        return plan
    statuses = [
        row[0] for row in db.query(IncidentAction.status)
        .filter(IncidentAction.response_plan_id == plan_id)
        .all()
    ]
    previous = plan.status
    if statuses and all(status == "completed" for status in statuses):
        plan.status = "completed"
    elif any(status in {"cancelled", "rejected"} for status in statuses):
        plan.status = "needs_review"
    else:
        plan.status = "active"
    if plan.status != previous:
        plan.updated_at = utcnow()
    return plan
