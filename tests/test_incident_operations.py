from __future__ import annotations

import importlib.util
import sys
import time
import types
from uuid import uuid4

if "redis" not in sys.modules and importlib.util.find_spec("redis") is None:
    redis_module = types.ModuleType("redis")
    exceptions_module = types.ModuleType("redis.exceptions")

    class RedisError(Exception):
        pass

    class Redis:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            raise RedisError("Redis is intentionally stubbed in this test")

    redis_module.Redis = Redis
    redis_module.exceptions = exceptions_module
    exceptions_module.RedisError = RedisError
    sys.modules["redis"] = redis_module
    sys.modules["redis.exceptions"] = exceptions_module

if "feedparser" not in sys.modules and importlib.util.find_spec("feedparser") is None:
    feedparser_module = types.ModuleType("feedparser")
    feedparser_module.parse = lambda *_args, **_kwargs: types.SimpleNamespace(bozo=False, entries=[], feed={})
    sys.modules["feedparser"] = feedparser_module

from fastapi.testclient import TestClient

from app.api import main as api_main
from app.core.security import _totp_at
from app.db.models import Incident
from app.db.session import SessionLocal


def test_life_safety_action_requires_explicit_approval_and_versioned_transitions(monkeypatch):
    api_main._rate_limit = lambda *_args, **_kwargs: None
    from app.api import incident_operations
    monkeypatch.setattr(incident_operations, "publish_event", lambda *_args, **_kwargs: None)

    with TestClient(api_main.app) as client:
        setup = client.get("/api/setup/status")
        if not setup.json()["setup_required"]:
            return
        created = client.post("/api/setup/admin", json={
            "email": "ops-test@cignal-test.com",
            "display_name": "Operations Test Admin",
            "password": "A-unique-test-password-2026!",
        })
        assert created.status_code == 201, created.text
        headers = {"Authorization": f"Bearer {created.json()['access_token']}"}
        started = client.post("/api/auth/mfa/start", headers=headers, json={})
        assert started.status_code == 200, started.text
        secret = started.json()["secret"]
        verified = client.post(
            "/api/auth/mfa/verify", headers=headers,
            json={"code": _totp_at(secret, int(time.time() // 30))},
        )
        assert verified.status_code == 200, verified.text

        with SessionLocal() as db:
            incident = Incident(
                id=str(uuid4()), incident_code="CIG-OPS-TEST",
                title="Verified flood incident", summary="Verified flood incident for operations tests.",
                disaster_type="flood", severity="high", status="verified",
                source_count=1, confidence=0.9, provenance="official",
            )
            db.add(incident)
            db.commit()
            incident_id = incident.id

        created_action = client.post(
            f"/api/incidents/{incident_id}/actions", headers=headers,
            json={
                "title": "Issue evacuation warning",
                "description": "Prepare a geographically scoped evacuation warning for review.",
                "action_type": "evacuation", "priority": "urgent",
            },
        )
        assert created_action.status_code == 201, created_action.text
        action = created_action.json()
        assert action["approval_required"] is True
        assert action["approval_status"] == "pending"
        assert action["status"] == "proposed"
        action_id = action["id"]

        bypass = client.patch(
            f"/api/incidents/{incident_id}/actions/{action_id}", headers=headers,
            json={"status": "approved", "expected_version": 1},
        )
        assert bypass.status_code == 409

        approval = client.post(
            f"/api/incidents/{incident_id}/actions/{action_id}/approval", headers=headers,
            json={"decision": "approve", "note": "Reviewed scope and supporting incident evidence."},
        )
        assert approval.status_code == 200, approval.text
        assert approval.json()["status"] == "approved"
        assert approval.json()["version"] == 2

        stale = client.patch(
            f"/api/incidents/{incident_id}/actions/{action_id}", headers=headers,
            json={"status": "in_progress", "expected_version": 1},
        )
        assert stale.status_code == 409

        started_action = client.patch(
            f"/api/incidents/{incident_id}/actions/{action_id}", headers=headers,
            json={"status": "in_progress", "expected_version": 2, "note": "Authorized execution started."},
        )
        assert started_action.status_code == 200, started_action.text
        completed = client.patch(
            f"/api/incidents/{incident_id}/actions/{action_id}", headers=headers,
            json={"status": "completed", "expected_version": 3, "note": "Execution completed and verified."},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["completed_at"] is not None

        events = client.get(
            f"/api/incidents/{incident_id}/actions/{action_id}/events", headers=headers,
        )
        assert events.status_code == 200
        kinds = [item["event_type"] for item in events.json()["items"]]
        assert {"created", "approval_approve", "status_changed"}.issubset(set(kinds))
