"""Role-aware delivery tests for the incident/notification WebSocket."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# Keep this module independently executable when optional service clients are absent.
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

from app.api import main as api_main
from app.core.security import create_access_token
from app.db.models import User
from app.db.session import SessionLocal
from app.services.websocket_auth import event_is_visible_to_user


@pytest.mark.parametrize("role", ["admin", "analyst", "responder"])
def test_operational_roles_receive_incident_events(role: str) -> None:
    assert event_is_visible_to_user(role, "user-1", {"type": "incident.updated", "payload": {}})
    assert event_is_visible_to_user(role, "user-1", {"type": "ingestion.completed", "payload": {}})
    assert event_is_visible_to_user(role, "user-1", {"type": "integration.run.completed", "payload": {}})


def test_citizen_does_not_receive_operational_broadcasts() -> None:
    assert not event_is_visible_to_user("citizen", "citizen-1", {"type": "incident.updated", "payload": {}})
    assert not event_is_visible_to_user("citizen", "citizen-1", {"type": "ingestion.completed", "payload": {}})
    assert not event_is_visible_to_user("citizen", "citizen-1", {"type": "integration.run.completed", "payload": {}})


@pytest.mark.parametrize("role", ["admin", "analyst", "responder", "citizen"])
def test_notifications_are_private_to_the_recipient(role: str) -> None:
    own = {"type": "notification.created", "payload": {"user_id": "user-1"}}
    other = {"type": "notification.created", "payload": {"user_id": "user-2"}}
    assert event_is_visible_to_user(role, "user-1", own)
    assert not event_is_visible_to_user(role, "user-1", other)


@pytest.mark.parametrize("event", [None, [], {}, {"type": None}, {"type": "unknown.event"}, {"type": "notification.created", "payload": []}])
def test_unknown_and_malformed_events_fail_closed(event) -> None:
    assert not event_is_visible_to_user("admin", "user-1", event)


class _OneEventPubSub:
    def __init__(self, event: dict):
        self.event = event
        self.sent = False

    def subscribe(self, *_channels):
        return None

    def get_message(self, *_args, **_kwargs):
        if not self.sent:
            self.sent = True
            return {"type": "message", "data": json.dumps(self.event)}
        raise RuntimeError("test stream complete")

    def close(self):
        return None


class _FakeRedis:
    def __init__(self, event: dict):
        self.stream = _OneEventPubSub(event)

    def pubsub(self):
        return self.stream

    def close(self):
        return None


@pytest.mark.parametrize(
    ("role", "event", "should_receive"),
    [
        ("admin", {"type": "incident.updated", "payload": {"incident_id": "synthetic-incident"}}, True),
        ("analyst", {"type": "incident.updated", "payload": {"incident_id": "synthetic-incident"}}, True),
        ("responder", {"type": "incident.updated", "payload": {"incident_id": "synthetic-incident"}}, True),
        ("admin", {"type": "incident.activity_added", "payload": {"incident_id": "synthetic-incident"}}, True),
        ("responder", {"type": "incident.activity_added", "payload": {"incident_id": "synthetic-incident"}}, True),
        ("analyst", {"type": "integration.run.completed", "payload": {"source_type": "usgs"}}, True),
        ("responder", {"type": "integration.run.completed", "payload": {"source_type": "usgs"}}, True),
        ("citizen", {"type": "integration.run.completed", "payload": {"source_type": "usgs"}}, False),
        ("citizen", {"type": "incident.activity_added", "payload": {"incident_id": "synthetic-incident"}}, False),
        ("citizen", {"type": "incident.updated", "payload": {"incident_id": "synthetic-incident"}}, False),
        ("citizen", {"type": "notification.created", "payload": {"user_id": "USER_ID"}}, True),
        ("citizen", {"type": "notification.created", "payload": {"user_id": "someone-else"}}, False),
    ],
)
def test_websocket_endpoint_enforces_event_visibility(monkeypatch, role: str, event: dict, should_receive: bool) -> None:
    user_id = str(uuid4())
    user = User(
        id=user_id,
        email=f"ws-{user_id}@cignal-test.invalid",
        display_name="WebSocket authorization test",
        password_hash="not-used-by-this-test",
        role=role,
        is_active=True,
        mfa_enabled=True,
    )
    with TestClient(api_main.app) as client:
        with SessionLocal() as db:
            db.add(user)
            db.commit()
        event = json.loads(json.dumps(event).replace("USER_ID", user_id))
        fake_redis = _FakeRedis(event)
        monkeypatch.setattr(api_main, "redis_client", lambda: fake_redis)
        token = create_access_token(user_id, role)
        with client.websocket_connect(f"/api/ws/incidents?token={token}") as websocket:
            if should_receive:
                assert websocket.receive_json() == event
            else:
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_json()
