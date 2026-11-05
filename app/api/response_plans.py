"""Human-supervised response playbook and dependency orchestration APIs."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_roles
from app.api.incident_operations import _action_out
from app.db.models import (
    Incident,
    IncidentAction,
    IncidentActionDependency,
    IncidentResponsePlan,
    User,
    utcnow,
)
from app.db.session import get_db
from app.services.audit import record_audit
from app.services.events import publish_event
from app.services.response_orchestration import dependency_blockers
from app.services.response_playbooks import get_playbook, list_playbooks

router = APIRouter(tags=["incident-response-plans"])


class ResponsePlanCreate(BaseModel):
    template_key: str = Field(pattern=r"^(flood|earthquake|fire|cyclone|landslide)_coordination_v1$")


def _plan_summary(db: Session, plan: IncidentResponsePlan) -> dict:
    actions = (
        db.query(IncidentAction)
        .filter(IncidentAction.response_plan_id == plan.id)
        .order_by(IncidentAction.sequence_order.asc(), IncidentAction.id.asc())
        .all()
    )
    states = [action.status for action in actions]
    return {
        "id": plan.id,
        "incident_id": plan.incident_id,
        "template_key": plan.template_key,
        "template_version": plan.template_version,
        "title": plan.title,
        "status": plan.status,
        "progress": {
            "total": len(actions),
            "completed": states.count("completed"),
            "in_progress": states.count("in_progress"),
            "blocked": states.count("blocked"),
            "cancelled": states.count("cancelled"),
            "rejected": states.count("rejected"),
            "not_started": sum(state in {"proposed", "approved"} for state in states),
        },
        "created_by": plan.created_by,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }


def _plan_detail(db: Session, plan: IncidentResponsePlan) -> dict:
    result = _plan_summary(db, plan)
    actions = (
        db.query(IncidentAction)
        .filter(IncidentAction.response_plan_id == plan.id)
        .order_by(IncidentAction.sequence_order.asc(), IncidentAction.id.asc())
        .all()
    )
    edges = (
        db.query(IncidentActionDependency)
        .filter(IncidentActionDependency.plan_id == plan.id)
        .order_by(IncidentActionDependency.action_id, IncidentActionDependency.depends_on_action_id)
        .all()
    )
    deps_by_action: dict[str, list[str]] = {}
    for edge in edges:
        deps_by_action.setdefault(edge.action_id, []).append(edge.depends_on_action_id)
    action_map = {action.id: action for action in actions}
    task_details = []
    for action in actions:
        prerequisites = [
            {
                "action_id": prerequisite_id,
                "title": action_map[prerequisite_id].title if prerequisite_id in action_map else "Missing prerequisite",
                "status": action_map[prerequisite_id].status if prerequisite_id in action_map else "missing",
            }
            for prerequisite_id in deps_by_action.get(action.id, [])
        ]
        blockers = dependency_blockers(db, plan.id, action.id)
        task = _action_out(action)
        task.update({
            "step_key": (plan.template_snapshot.get("steps", [])[action.sequence_order].get("step_key")
                         if action.sequence_order < len(plan.template_snapshot.get("steps", [])) else None),
            "depends_on": prerequisites,
            "blocking_actions": blockers,
            "prerequisites_complete": not blockers,
            "ready_for_work": (
                plan.status == "active"
                and action.status == "approved"
                and not blockers
                and (not action.approval_required or action.approval_status == "approved")
            ),
        })
        task_details.append(task)
    result["template_snapshot"] = plan.template_snapshot
    result["actions"] = task_details
    result["policy"] = {
        "human_review_required": True,
        "external_side_effects": False,
        "automatic_incident_creation": False,
        "automatic_warning_or_dispatch": False,
    }
    return result


@router.get("/api/response-playbooks")
def get_response_playbooks(
    disaster_type: str | None = Query(default=None, pattern="^(flood|earthquake|fire|cyclone|landslide)$"),
    user: User = Depends(require_roles("admin", "analyst", "responder")),
):
    return list_playbooks(disaster_type)


@router.post("/api/incidents/{incident_id}/response-plans", status_code=201)
def create_response_plan(
    incident_id: str,
    payload: ResponsePlanCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    if not idempotency_key.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key):
        raise HTTPException(status_code=422, detail="Idempotency-Key must contain printable ASCII without spaces")
    incident = db.query(Incident).filter_by(id=incident_id).with_for_update().first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Replay a prior request before rechecking mutable incident state or catalogue
    # contents. Idempotency must remain stable after the plan advances or closes.
    existing = db.query(IncidentResponsePlan).filter_by(
        incident_id=incident.id, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.template_key != payload.template_key:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for a different playbook")
        response.status_code = 200
        return {**_plan_detail(db, existing), "idempotent_replay": True}

    if incident.status not in {"verified", "monitoring"}:
        raise HTTPException(status_code=409, detail="Only verified or monitoring incidents can receive a new response plan")

    template = get_playbook(payload.template_key)
    if template is None:
        raise HTTPException(status_code=422, detail="Unknown response playbook template")
    if template["disaster_type"] != incident.disaster_type:
        raise HTTPException(
            status_code=422,
            detail={"message": "Playbook disaster type does not match the incident.",
                    "incident_disaster_type": incident.disaster_type,
                    "template_disaster_type": template["disaster_type"]},
        )

    active = db.query(IncidentResponsePlan).filter_by(
        incident_id=incident.id, status="active"
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "An active response plan already exists for this incident.",
                    "active_plan_id": active.id},
        )

    now = utcnow()
    plan = IncidentResponsePlan(
        id=str(uuid4()), incident_id=incident.id,
        template_key=template["template_key"], template_version=template["template_version"],
        title=template["title"], status="active", idempotency_key=idempotency_key,
        template_snapshot=template, created_by=user.id, created_at=now, updated_at=now,
    )
    db.add(plan)
    db.flush()

    from app.api.incident_operations import APPROVAL_GATED_TYPES, _append_event

    actions_by_key: dict[str, IncidentAction] = {}
    for sequence, step in enumerate(template["steps"]):
        gated = step["action_type"] in APPROVAL_GATED_TYPES
        action = IncidentAction(
            id=str(uuid4()), incident_id=incident.id, response_plan_id=plan.id,
            sequence_order=sequence, title=step["title"], description=step["description"],
            action_type=step["action_type"], priority=step["priority"], status="proposed",
            approval_required=gated, approval_status="pending" if gated else "not_required",
            created_by=user.id, version=1, created_at=now, updated_at=now,
        )
        db.add(action)
        db.flush()
        _append_event(
            db, action, user, "planned", to_status="proposed",
            event_data={"plan_id": plan.id, "template_key": plan.template_key,
                        "template_version": plan.template_version, "step_key": step["step_key"]},
        )
        actions_by_key[step["step_key"]] = action

    for step in template["steps"]:
        action = actions_by_key[step["step_key"]]
        for prerequisite_key in step["depends_on"]:
            prerequisite = actions_by_key[prerequisite_key]
            db.add(IncidentActionDependency(
                id=str(uuid4()), plan_id=plan.id, action_id=action.id,
                depends_on_action_id=prerequisite.id, created_at=now,
            ))

    incident.updated_at = now
    record_audit(
        db, "incident.response_plan_created", "incident_response_plan", plan.id,
        user.id, request,
        {"incident_id": incident.id, "template_key": plan.template_key,
         "template_version": plan.template_version, "action_count": len(actions_by_key)},
    )
    try:
        db.commit()
    except IntegrityError as err:
        db.rollback()
        raced = db.query(IncidentResponsePlan).filter_by(
            incident_id=incident.id, idempotency_key=idempotency_key
        ).first()
        if raced is not None and raced.template_key == payload.template_key:
            response.status_code = 200
            return {**_plan_detail(db, raced), "idempotent_replay": True}
        raise HTTPException(status_code=409, detail="A response plan was concurrently created; reload the incident") from err

    db.refresh(plan)
    publish_event("incident.response_plan_created", {"incident_id": incident.id, "plan_id": plan.id})
    return {**_plan_detail(db, plan), "idempotent_replay": False}


@router.get("/api/incidents/{incident_id}/response-plans")
def list_incident_response_plans(
    incident_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    if db.get(Incident, incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    plans = (
        db.query(IncidentResponsePlan).filter_by(incident_id=incident_id)
        .order_by(IncidentResponsePlan.created_at.desc(), IncidentResponsePlan.id.desc())
        .limit(limit).all()
    )
    return [_plan_summary(db, plan) for plan in plans]


@router.get("/api/incidents/{incident_id}/response-plans/{plan_id}")
def get_incident_response_plan(
    incident_id: str,
    plan_id: str,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    plan = db.query(IncidentResponsePlan).filter_by(id=plan_id, incident_id=incident_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Response plan not found")
    return _plan_detail(db, plan)


@router.post("/api/incidents/{incident_id}/response-plans/{plan_id}/cancel")
def cancel_incident_response_plan(
    incident_id: str,
    plan_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    plan = db.query(IncidentResponsePlan).filter_by(
        id=plan_id, incident_id=incident_id
    ).with_for_update().first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Response plan not found")
    if plan.status not in {"active", "needs_review"}:
        raise HTTPException(status_code=409, detail=f"A {plan.status} plan cannot be cancelled")
    in_progress = (
        db.query(IncidentAction.id)
        .filter_by(response_plan_id=plan.id, status="in_progress")
        .all()
    )
    if in_progress:
        raise HTTPException(
            status_code=409,
            detail={"message": "Track or block in-progress tasks before cancelling the plan.",
                    "in_progress_action_ids": [row[0] for row in in_progress]},
        )
    plan.status = "cancelled"
    plan.updated_at = utcnow()
    record_audit(
        db, "incident.response_plan_cancelled", "incident_response_plan", plan.id,
        user.id, request, {"incident_id": incident_id},
    )
    db.commit()
    db.refresh(plan)
    publish_event("incident.response_plan_cancelled", {"incident_id": incident_id, "plan_id": plan.id})
    return _plan_detail(db, plan)
