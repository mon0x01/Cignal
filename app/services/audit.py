from uuid import uuid4

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models import AuditLog


def record_audit(
    db: Session,
    action: str,
    resource_type: str,
    resource_id: str | None,
    actor_id: str | None = None,
    request: Request | None = None,
    details: dict | None = None,
) -> None:
    db.add(AuditLog(
        id=str(uuid4()), actor_user_id=actor_id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent", "")[:300] if request else None,
        details=details or {},
    ))
