"""Authorization policy for events delivered over the authenticated WebSocket."""
from __future__ import annotations

from typing import Any

INCIDENT_EVENT_ROLES = frozenset({"admin", "analyst", "responder"})


def event_is_visible_to_user(role: str, user_id: str, event: Any) -> bool:
    """Return whether an event may be delivered to this user.

    Operational incident and ingestion events are visible to response staff.
    Notifications are private and visible only to their intended recipient.
    Unknown or malformed events fail closed.
    """
    if not isinstance(event, dict):
        return False

    event_type = event.get("type")
    if not isinstance(event_type, str):
        return False

    if event_type.startswith(("incident.", "integration.")) or event_type == "ingestion.completed":
        return role in INCIDENT_EVENT_ROLES

    if event_type.startswith("notification."):
        payload = event.get("payload")
        return isinstance(payload, dict) and payload.get("user_id") == user_id

    return False
