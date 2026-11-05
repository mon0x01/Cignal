"""Incident operations API: durable tasks, approval gates and append-only history."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.dependencies import require_roles
from app.api.schemas import (
    IncidentActionApproval,
    IncidentActionCreate,
    IncidentActionStatusUpdate,
)
from app.db.models import Incident, IncidentAction, IncidentActionEvent, User, utcnow
from app.db.session import get_db
from app.services.audit import record_audit
from app.services.events import publish_event

router = APIRouter(prefix="/api/incidents", tags=["incident-operations"])

APPROVAL_GATED_TYPES = {
    "public_warning", "evacuation", "resource_dispatch", "medical_intervention"
}


def _action_out(action: IncidentAction) -> dict:
    return {
        "id": action.id,
        "incident_id": action.incident_id,
        "response_plan_id": action.response_plan_id,
        "sequence_order": action.sequence_order,
        "title": action.title,
        "description": action.description,
        "action_type": action.action_type,
        "priority": action.priority,
        "status": action.status,
        "approval_required": action.approval_required,
        "approval_status": action.approval_status,
        "assigned_to": action.assigned_to,
        "created_by": action.created_by,
        "approved_by": action.approved_by,
        "approval_note": action.approval_note,
        "due_at": action.due_at.isoformat() if action.due_at else None,
        "completed_at": action.completed_at.isoformat() if action.completed_at else None,
        "version": action.version,
        "created_at": action.created_at.isoformat(),
        "updated_at": action.updated_at.isoformat(),
    }


def _event_out(event: IncidentActionEvent) -> dict:
    return {
        "id": event.id,
        "action_id": event.action_id,
        "actor_user_id": event.actor_user_id,
        "event_type": event.event_type,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "note": event.note,
        "event_data": event.event_data,
        "created_at": event.created_at.isoformat(),
    }


def _append_event(db: Session, action: IncidentAction, actor: User, event_type: str,
                  from_status: str | None = None, to_status: str | None = None,
                  note: str | None = None, event_data: dict | None = None) -> IncidentActionEvent:
    event = IncidentActionEvent(
        id=str(uuid4()), action_id=action.id, actor_user_id=actor.id,
        event_type=event_type, from_status=from_status, to_status=to_status,
        note=note, event_data=event_data or {}, created_at=utcnow(),
    )
    db.add(event)
    return event


@router.post("/{incident_id}/actions", status_code=201)
def create_incident_action(
    incident_id: str, payload: IncidentActionCreate, request: Request,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    incident = db.query(Incident).filter(Incident.id == incident_id).with_for_update().first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status in {"resolved", "closed"}:
        raise HTTPException(status_code=409, detail="Resolved or closed incidents cannot accept new actions")
    if payload.assigned_to:
        assignee = db.get(User, payload.assigned_to)
        if not assignee or not assignee.is_active or assignee.role not in {"admin", "analyst", "responder"}:
            raise HTTPException(status_code=422, detail="assigned_to must identify an operational user")
    gated = payload.action_type in APPROVAL_GATED_TYPES
    action = IncidentAction(
        id=str(uuid4()), incident_id=incident.id, title=payload.title,
        description=payload.description, action_type=payload.action_type,
        priority=payload.priority, status="proposed",
        approval_required=gated, approval_status="pending" if gated else "not_required",
        assigned_to=payload.assigned_to, created_by=user.id, due_at=payload.due_at,
        version=1, created_at=utcnow(), updated_at=utcnow(),
    )
    db.add(action)
    _append_event(
        db, action, user, "created", to_status="proposed",
        event_data={"approval_required": gated, "action_type": action.action_type},
    )
    incident.updated_at = utcnow()
    record_audit(db, "incident.action_created", "incident_action", action.id, user.id, request,
                 {"incident_id": incident.id, "action_type": action.action_type,
                  "approval_required": gated})
    db.commit()
    db.refresh(action)
    publish_event("incident.action_created", {"incident_id": incident.id, "action_id": action.id})
    return _action_out(action)


@router.get("/{incident_id}/actions")
def list_incident_actions(
    incident_id: str, status: str | None = Query(default=None, pattern="^(proposed|approved|in_progress|blocked|completed|cancelled|rejected)$"),
    limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0, le=10000),
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    if not db.get(Incident, incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    query = db.query(IncidentAction).filter(IncidentAction.incident_id == incident_id)
    if status:
        query = query.filter(IncidentAction.status == status)
    total = query.count()
    rows = query.order_by(IncidentAction.created_at.desc(), IncidentAction.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_action_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{incident_id}/actions/{action_id}/events")
def list_incident_action_events(
    incident_id: str, action_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    action = db.query(IncidentAction).filter_by(id=action_id, incident_id=incident_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Incident action not found")
    rows = db.query(IncidentActionEvent).filter_by(action_id=action.id).order_by(
        IncidentActionEvent.created_at.desc(), IncidentActionEvent.id.desc()
    ).limit(limit).all()
    return {"items": [_event_out(row) for row in rows]}


@router.post("/{incident_id}/actions/{action_id}/approval")
def decide_incident_action_approval(
    incident_id: str, action_id: str, payload: IncidentActionApproval, request: Request,
    user: User = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db),
):
    action = db.query(IncidentAction).filter_by(
        id=action_id, incident_id=incident_id
    ).with_for_update().first()
    if not action:
        raise HTTPException(status_code=404, detail="Incident action not found")
    if not action.approval_required:
        raise HTTPException(status_code=409, detail="This action does not require an approval gate")
    if action.response_plan_id:
        from app.db.models import IncidentResponsePlan

        plan = db.query(IncidentResponsePlan).filter_by(
            id=action.response_plan_id
        ).with_for_update().first()
        if plan is None or plan.status != "active":
            raise HTTPException(
                status_code=409,
                detail={"message": "Tasks in a non-active response plan cannot be approved.",
                        "plan_status": plan.status if plan else "missing"},
            )
    if action.approval_status != "pending" or action.status != "proposed":
        raise HTTPException(status_code=409, detail="Only pending proposed actions can be reviewed")
    old_status = action.status
    action.approval_status = "approved" if payload.decision == "approve" else "rejected"
    action.status = "approved" if payload.decision == "approve" else "rejected"
    action.approved_by = user.id
    action.approval_note = payload.note
    action.version += 1
    action.updated_at = utcnow()
    _append_event(db, action, user, f"approval_{payload.decision}", old_status,
                  action.status, payload.note, {"approval_status": action.approval_status})
    record_audit(db, f"incident.action_approval_{payload.decision}", "incident_action",
                 action.id, user.id, request, {"incident_id": incident_id})
    if action.response_plan_id and payload.decision == "reject":
        from app.services.response_orchestration import refresh_response_plan_status
        refresh_response_plan_status(db, action.response_plan_id)
    db.commit()
    db.refresh(action)
    publish_event("incident.action_updated", {"incident_id": incident_id, "action_id": action.id,
                                               "status": action.status})
    return _action_out(action)


@router.patch("/{incident_id}/actions/{action_id}")
def update_incident_action_status(
    incident_id: str, action_id: str, payload: IncidentActionStatusUpdate, request: Request,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    action = db.query(IncidentAction).filter_by(
        id=action_id, incident_id=incident_id
    ).with_for_update().first()
    if not action:
        raise HTTPException(status_code=404, detail="Incident action not found")
    if payload.expected_version != action.version:
        raise HTTPException(status_code=409, detail={
            "message": "Action was modified by another operator; reload before retrying",
            "current_version": action.version,
        })
    if action.status in {"completed", "cancelled", "rejected"}:
        raise HTTPException(status_code=409, detail="Terminal actions cannot be changed")
    if action.response_plan_id:
        from app.db.models import IncidentResponsePlan

        plan = db.query(IncidentResponsePlan).filter_by(
            id=action.response_plan_id
        ).with_for_update().first()
        if plan is None or plan.status != "active":
            raise HTTPException(
                status_code=409,
                detail={"message": "Tasks in a non-active response plan cannot be changed.",
                        "plan_status": plan.status if plan else "missing"},
            )
    allowed = {
        "proposed": {"approved", "cancelled"},
        "approved": {"in_progress", "cancelled"},
        "in_progress": {"blocked", "completed", "cancelled"},
        "blocked": {"in_progress", "cancelled"},
    }
    target = payload.status
    if target not in allowed.get(action.status, set()):
        raise HTTPException(status_code=409, detail=f"Invalid action transition: {action.status} -> {target}")
    if target == "approved" and action.approval_required:
        raise HTTPException(status_code=409, detail="This action requires the dedicated approval endpoint")
    if action.approval_required and action.approval_status != "approved" and target == "in_progress":
        raise HTTPException(status_code=409, detail="Action cannot start before explicit approval")
    if target == "in_progress" and action.response_plan_id:
        from app.db.models import IncidentResponsePlan
        from app.services.response_orchestration import dependency_blockers

        plan = db.query(IncidentResponsePlan).filter_by(
            id=action.response_plan_id
        ).with_for_update().first()
        if plan is None or plan.status != "active":
            raise HTTPException(
                status_code=409,
                detail={"message": "This response plan is not active; review or cancel it before continuing.",
                        "plan_status": plan.status if plan else "missing"},
            )
        blockers = dependency_blockers(db, plan.id, action.id)
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Action prerequisites must be completed before this task can start.",
                    "blocking_actions": blockers,
                },
            )
    old_status = action.status
    action.status = target
    action.version += 1
    action.updated_at = utcnow()
    if target == "completed":
        action.completed_at = utcnow()
    _append_event(db, action, user, "status_changed", old_status, target, payload.note,
                  {"version": action.version})
    record_audit(db, "incident.action_status_changed", "incident_action", action.id,
                 user.id, request, {"from": old_status, "to": target, "version": action.version})
    if action.response_plan_id and target in {"completed", "cancelled"}:
        from app.services.response_orchestration import refresh_response_plan_status
        refresh_response_plan_status(db, action.response_plan_id)
    db.commit()
    db.refresh(action)
    publish_event("incident.action_updated", {"incident_id": incident_id, "action_id": action.id,
                                               "status": action.status})
    return _action_out(action)
