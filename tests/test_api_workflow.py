"""End-to-end API smoke test with in-memory stand-ins for Redis/feedparser.

The production Compose stack uses real Redis and feedparser. Stubs here keep this
workflow test executable in a restricted/offline test environment without making
external network calls.
"""
from __future__ import annotations

import importlib.util
import sys
import time
import types
from uuid import uuid4

import pytest

if importlib.util.find_spec("redis") is None:
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

if importlib.util.find_spec("feedparser") is None:
    feedparser_module = types.ModuleType("feedparser")
    feedparser_module.parse = lambda *_args, **_kwargs: types.SimpleNamespace(bozo=False, entries=[], feed={})
    sys.modules["feedparser"] = feedparser_module

from fastapi.testclient import TestClient

from app.api import main as api_main
from app.core.security import _totp_at


@pytest.fixture(scope="module")
def client():
    api_main._rate_limit = lambda *_args, **_kwargs: None
    api_main.enqueue_job = lambda *_args, **_kwargs: None
    api_main.publish_event = lambda *_args, **_kwargs: None
    with TestClient(api_main.app) as test_client:
        yield test_client


def test_admin_mfa_report_review_and_incident_workflow(client, monkeypatch):
    setup = client.get("/api/setup/status")
    if not setup.json()["setup_required"]:
        pytest.skip("The isolated test database already contains an account")

    created = client.post("/api/setup/admin", json={
        "email": "workflow-admin@cignal-test.com",
        "display_name": "Workflow Administrator",
        "password": "A-unique-test-password-2026!",
    })
    assert created.status_code == 201, created.text
    token = created.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    started = client.post("/api/auth/mfa/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    counter = int(time.time() // 30)
    verified = client.post("/api/auth/mfa/verify", headers=headers, json={"code": _totp_at(secret, counter)})
    assert verified.status_code == 200, verified.text
    assert len(verified.json()["recovery_codes"]) == 8

    source_health = client.get("/api/integrations/health", headers=headers)
    assert source_health.status_code == 200, source_health.text
    assert isinstance(source_health.json(), list)
    source_runs = client.get("/api/integrations/runs", headers=headers)
    assert source_runs.status_code == 200, source_runs.text
    assert isinstance(source_runs.json(), list)

    # Event discovery is analyst/admin-only and remains a background, reviewable
    # hypothesis workflow; requesting it must not create a verified Incident.
    discovery = client.post("/api/intelligence/event-discoveries", headers=headers, json={})
    assert discovery.status_code == 202, discovery.text
    discovery_run_id = discovery.json()["id"]
    assert discovery.json()["status"] == "queued"
    assert discovery.json()["trigger_kind"] == "analyst"
    assert discovery.json()["dispatch_status"] == "pending"
    assert discovery.json()["job_id"] == f"cignal-event-discovery-{discovery_run_id}"
    from app.db.models import BackgroundJobOutbox
    from app.db.session import SessionLocal
    with SessionLocal() as outbox_check_db:
        durable_request = outbox_check_db.query(BackgroundJobOutbox).filter_by(
            job_id=discovery.json()["job_id"]
        ).one()
        assert durable_request.status == "pending"
        assert durable_request.function_path == "app.worker.discover_event_hypotheses"
    discovery_status = client.get(
        f"/api/intelligence/event-discoveries/{discovery_run_id}", headers=headers
    )
    assert discovery_status.status_code == 200
    assert discovery_status.json()["status"] == "queued"
    hypotheses = client.get("/api/intelligence/event-hypotheses", headers=headers)
    assert hypotheses.status_code == 200
    assert isinstance(hypotheses.json(), list)
    semantic_status = client.get("/api/intelligence/semantic-status", headers=headers)
    assert semantic_status.status_code == 200
    assert "multilingual_embeddings" in semantic_status.json()
    assert "supervised_event_pair_model" in semantic_status.json()
    source_profiles = client.get("/api/intelligence/source-reliability", headers=headers)
    assert source_profiles.status_code == 200
    assert isinstance(source_profiles.json(), list)
    profile_refresh = client.post(
        "/api/intelligence/source-reliability/refresh", headers=headers
    )
    assert profile_refresh.status_code == 200, profile_refresh.text

    idempotency_key = str(uuid4())
    payload = {
        "title": "Flooding near central market",
        "description": "Water is rising quickly around the market and nearby homes need assistance.",
        "disaster_type": "flood",
        "urgency": "high",
        "latitude": 23.0225,
        "longitude": 72.5714,
        "location_text": "Ahmedabad, Gujarat",
        "language": "en",
    }
    submitted = client.post("/api/reports", headers={**headers, "Idempotency-Key": idempotency_key}, json=payload)
    assert submitted.status_code == 201, submitted.text
    repeated = client.post("/api/reports", headers={**headers, "Idempotency-Key": idempotency_key}, json=payload)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == submitted.json()["id"]
    outbox_status = client.get("/api/admin/background-jobs/outbox", headers=headers)
    assert outbox_status.status_code == 200, outbox_status.text
    assert outbox_status.json()["counts"]["pending"] >= 1
    from app.db.models import BackgroundJobOutbox, utcnow
    from app.db.session import SessionLocal
    retry_report_id = submitted.json()["id"]
    retry_job_id = f"cignal-report-analysis-{retry_report_id}-r99"
    with SessionLocal() as session:
        session.add(BackgroundJobOutbox(
            id=str(uuid4()), job_id=retry_job_id, function_path="app.worker.process_report",
            task_args=[retry_report_id, 99], queue_name="cignal", job_timeout_seconds=300,
            result_ttl_seconds=3600, status="dead", attempts=12,
            available_at=utcnow(), last_error_code="ConnectionError",
            created_at=utcnow(), updated_at=utcnow(),
        ))
        session.commit()
    retried = client.post(
        f"/api/admin/background-jobs/outbox/{retry_job_id}/retry", headers=headers
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "pending"
    with SessionLocal() as session:
        released = session.query(BackgroundJobOutbox).filter_by(job_id=retry_job_id).one()
        assert released.attempts == 0
        assert released.last_error_code is None
    collision = client.post("/api/reports", headers={"Idempotency-Key": idempotency_key}, json=payload)
    assert collision.status_code == 409

    report_id = submitted.json()["id"]
    from app.db.models import BackgroundJobOutbox, Report
    from app.db.session import SessionLocal
    with SessionLocal() as session:
        durable_job = session.query(BackgroundJobOutbox).filter_by(
            job_id=f"cignal-report-analysis-{report_id}-r1"
        ).one()
        assert durable_job.status == "pending"
        assert durable_job.task_args == [report_id, 1]
        # Repeating the API idempotency key must not create another durable job.
        assert session.query(BackgroundJobOutbox).filter_by(
            job_id=f"cignal-report-analysis-{report_id}-r1"
        ).count() == 1
        stored_report = session.get(Report, report_id)
        stored_report.ai_analysis = {"pipeline_version": "test", "human_review_required": True}
        session.commit()
    analysis_response = client.get(f"/api/reports/{report_id}/analysis", headers=headers)
    assert analysis_response.status_code == 200
    assert analysis_response.json()["analysis"]["human_review_required"] is True
    reviewed = client.post(f"/api/reports/{report_id}/review", headers=headers, json={"decision": "verify"})
    assert reviewed.status_code == 200, reviewed.text
    incident = reviewed.json()["incident"]
    assert incident["disaster_type"] == "flood"
    assert incident["severity"] == "high"
    assert incident["source_count"] == 1

    duplicate_decision = client.post(f"/api/reports/{report_id}/review", headers=headers, json={"decision": "verify"})
    assert duplicate_decision.status_code == 409
    incident_rows = client.get("/api/incidents", headers=headers)
    assert incident_rows.status_code == 200
    assert any(row["id"] == incident["id"] for row in incident_rows.json())

    # A responder can see verified incidents, but must never browse unreviewed reports.
    responder_password = "Responder-test-password-2026!"
    responder_created = client.post("/api/users", headers=headers, json={
        "email": "workflow-responder@cignal-test.com",
        "display_name": "Workflow Responder",
        "password": responder_password,
        "role": "responder",
    })
    assert responder_created.status_code == 201, responder_created.text
    responder_login = client.post("/api/auth/login", json={
        "email": "workflow-responder@cignal-test.com", "password": responder_password,
    })
    assert responder_login.status_code == 200, responder_login.text
    responder_headers = {"Authorization": f"Bearer {responder_login.json()['access_token']}"}
    responder_mfa = client.post("/api/auth/mfa/start", headers=responder_headers, json={})
    responder_secret = responder_mfa.json()["secret"]
    responder_verified = client.post("/api/auth/mfa/verify", headers=responder_headers, json={
        "code": _totp_at(responder_secret, int(time.time() // 30)),
    })
    assert responder_verified.status_code == 200, responder_verified.text
    assert client.get("/api/admin/background-jobs/outbox", headers=responder_headers).status_code == 403
    assert client.get("/api/integrations/health", headers=responder_headers).status_code == 403
    assert client.get("/api/integrations/runs", headers=responder_headers).status_code == 403
    assert client.get("/api/intelligence/event-hypotheses", headers=responder_headers).status_code == 403
    assert client.get("/api/intelligence/semantic-status", headers=responder_headers).status_code == 403
    assert client.get("/api/intelligence/source-reliability", headers=responder_headers).status_code == 403
    assert client.post("/api/intelligence/event-discoveries", headers=responder_headers, json={}).status_code == 403

    # Citizen can only see their own report and can answer an analyst's request.
    citizen_password = "Citizen-test-password-2026!"
    citizen_created = client.post("/api/users", headers=headers, json={
        "email": "workflow-citizen@cignal-test.com",
        "display_name": "Workflow Citizen",
        "password": citizen_password,
        "role": "citizen",
    })
    assert citizen_created.status_code == 201, citizen_created.text
    citizen_login = client.post("/api/auth/login", json={
        "email": "workflow-citizen@cignal-test.com", "password": citizen_password,
    })
    assert citizen_login.status_code == 200, citizen_login.text
    citizen_headers = {"Authorization": f"Bearer {citizen_login.json()['access_token']}"}
    assert client.get(f"/api/reports/{report_id}/analysis", headers=citizen_headers).status_code == 403
    assert citizen_login.json()["mfa_setup_required"] is True
    citizen_mfa = client.post("/api/auth/mfa/start", headers=citizen_headers, json={})
    citizen_secret = citizen_mfa.json()["secret"]
    citizen_verified = client.post("/api/auth/mfa/verify", headers=citizen_headers, json={
        "code": _totp_at(citizen_secret, int(time.time() // 30)),
    })
    assert citizen_verified.status_code == 200, citizen_verified.text
    assert client.get("/api/integrations/health", headers=citizen_headers).status_code == 403
    assert client.get("/api/integrations/runs", headers=citizen_headers).status_code == 403

    citizen_report = client.post("/api/reports", headers=citizen_headers, json={
        "title": "Road access blocked after heavy rain",
        "description": "Water and debris are blocking the road near the bus terminal. Residents request an update.",
        "disaster_type": "flood", "urgency": "moderate", "language": "gu",
    })
    assert citizen_report.status_code == 201, citizen_report.text
    citizen_report_id = citizen_report.json()["id"]
    requested = client.post(f"/api/reports/{citizen_report_id}/review", headers=headers, json={
        "decision": "request_info", "note": "Please confirm whether the road is still blocked and share a safe location description.",
    })
    assert requested.status_code == 200, requested.text
    citizen_notifications = client.get("/api/notifications", headers=citizen_headers)
    assert citizen_notifications.status_code == 200
    assert any(item["title"] == "Additional information requested" for item in citizen_notifications.json())
    own_reports = client.get("/api/reports", headers=citizen_headers)
    assert own_reports.status_code == 200
    assert any(row["id"] == citizen_report_id and row["status"] == "needs_info" for row in own_reports.json())
    followup = client.post(f"/api/reports/{citizen_report_id}/followup", headers=citizen_headers, json={
        "message": "The water is now receding and the road is passable for small vehicles.",
    })
    assert followup.status_code == 200, followup.text
    assert followup.json()["status"] == "submitted"
    assert "Citizen follow-up" in followup.json()["description"]

    # Link a second report to an existing open incident. Association and source_count
    # must update once, and the incident detail must preserve the report relationship.
    linked = client.post(f"/api/reports/{citizen_report_id}/review", headers=headers, json={
        "decision": "link", "incident_id": incident["id"], "note": "Same flood event; citizen follow-up supports the incident.",
    })
    assert linked.status_code == 200, linked.text
    assert linked.json()["report"]["status"] == "verified"
    assert linked.json()["incident"]["source_count"] == 2

    duplicate_link = client.post(f"/api/reports/{citizen_report_id}/review", headers=headers, json={
        "decision": "link", "incident_id": incident["id"],
    })
    assert duplicate_link.status_code == 409
    missing_target = client.post(f"/api/reports/{report_id}/review", headers=headers, json={"decision": "link"})
    assert missing_target.status_code == 422

    incident_detail = client.get(f"/api/incidents/{incident['id']}", headers=headers)
    assert incident_detail.status_code == 200, incident_detail.text
    assert len(incident_detail.json()["report_links"]) == 2
    assert all(item["evidence_count"] == 0 for item in incident_detail.json()["report_links"])
    assert {item["report"]["id"] for item in incident_detail.json()["report_links"]} == {report_id, citizen_report_id}

    # Response playbooks create an idempotent, versioned plan. Dependencies and
    # approval gates must be satisfied before a task can enter in_progress.
    playbooks = client.get("/api/response-playbooks", headers=headers, params={"disaster_type": "flood"})
    assert playbooks.status_code == 200, playbooks.text
    assert len(playbooks.json()) == 1
    assert playbooks.json()[0]["template_key"] == "flood_coordination_v1"
    plan_key = str(uuid4())
    plan_headers = {**headers, "Idempotency-Key": plan_key}
    plan_response = client.post(
        f"/api/incidents/{incident['id']}/response-plans", headers=plan_headers,
        json={"template_key": "flood_coordination_v1"},
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    assert plan["status"] == "active"
    assert plan["idempotent_replay"] is False
    assert len(plan["actions"]) == 5
    assert plan["actions"][4]["approval_required"] is True
    assert plan["actions"][4]["approval_status"] == "pending"
    replayed_plan = client.post(
        f"/api/incidents/{incident['id']}/response-plans", headers=plan_headers,
        json={"template_key": "flood_coordination_v1"},
    )
    assert replayed_plan.status_code == 200, replayed_plan.text
    assert replayed_plan.json()["id"] == plan["id"]
    assert replayed_plan.json()["idempotent_replay"] is True
    reused_key_conflict = client.post(
        f"/api/incidents/{incident['id']}/response-plans", headers=plan_headers,
        json={"template_key": "cyclone_coordination_v1"},
    )
    assert reused_key_conflict.status_code == 409
    listed_plans = client.get(f"/api/incidents/{incident['id']}/response-plans", headers=headers)
    assert listed_plans.status_code == 200
    assert any(item["id"] == plan["id"] for item in listed_plans.json())
    active_conflict = client.post(
        f"/api/incidents/{incident['id']}/response-plans",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"template_key": "flood_coordination_v1"},
    )
    assert active_conflict.status_code == 409
    wrong_template = client.post(
        f"/api/incidents/{incident['id']}/response-plans",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"template_key": "cyclone_coordination_v1"},
    )
    assert wrong_template.status_code == 422

    plan_actions = plan["actions"]
    first_task, second_task = plan_actions[0], plan_actions[1]
    approved_second = client.patch(
        f"/api/incidents/{incident['id']}/actions/{second_task['id']}", headers=headers,
        json={"status": "approved", "expected_version": 1},
    )
    assert approved_second.status_code == 200, approved_second.text
    blocked_second = client.patch(
        f"/api/incidents/{incident['id']}/actions/{second_task['id']}", headers=headers,
        json={"status": "in_progress", "expected_version": 2},
    )
    assert blocked_second.status_code == 409
    assert blocked_second.json()["detail"]["blocking_actions"][0]["action_id"] == first_task["id"]

    first_approved = client.patch(
        f"/api/incidents/{incident['id']}/actions/{first_task['id']}", headers=headers,
        json={"status": "approved", "expected_version": 1},
    )
    assert first_approved.status_code == 200, first_approved.text
    first_started = client.patch(
        f"/api/incidents/{incident['id']}/actions/{first_task['id']}", headers=headers,
        json={"status": "in_progress", "expected_version": 2},
    )
    assert first_started.status_code == 200, first_started.text
    first_completed = client.patch(
        f"/api/incidents/{incident['id']}/actions/{first_task['id']}", headers=headers,
        json={"status": "completed", "expected_version": 3},
    )
    assert first_completed.status_code == 200, first_completed.text
    second_started = client.patch(
        f"/api/incidents/{incident['id']}/actions/{second_task['id']}", headers=headers,
        json={"status": "in_progress", "expected_version": 2},
    )
    assert second_started.status_code == 200, second_started.text
    cancel_with_in_progress = client.post(
        f"/api/incidents/{incident['id']}/response-plans/{plan['id']}/cancel", headers=headers,
    )
    assert cancel_with_in_progress.status_code == 409
    second_blocked = client.patch(
        f"/api/incidents/{incident['id']}/actions/{second_task['id']}", headers=headers,
        json={"status": "blocked", "expected_version": 3},
    )
    assert second_blocked.status_code == 200, second_blocked.text
    plan_detail = client.get(
        f"/api/incidents/{incident['id']}/response-plans/{plan['id']}", headers=headers,
    )
    assert plan_detail.status_code == 200, plan_detail.text
    assert plan_detail.json()["actions"][1]["prerequisites_complete"] is True
    assert plan_detail.json()["progress"]["completed"] == 1
    close_with_active_plan = client.patch(
        f"/api/incidents/{incident['id']}", headers=headers, json={"status": "resolved"},
    )
    assert close_with_active_plan.status_code == 409

    cancelled_plan = client.post(
        f"/api/incidents/{incident['id']}/response-plans/{plan['id']}/cancel", headers=headers,
    )
    assert cancelled_plan.status_code == 200, cancelled_plan.text
    assert cancelled_plan.json()["status"] == "cancelled"
    third_task = plan_actions[2]
    cancelled_plan_update = client.patch(
        f"/api/incidents/{incident['id']}/actions/{third_task['id']}", headers=headers,
        json={"status": "approved", "expected_version": 1},
    )
    assert cancelled_plan_update.status_code == 409
    cancelled_warning_approval = client.post(
        f"/api/incidents/{incident['id']}/actions/{plan_actions[4]['id']}/approval",
        headers=headers, json={"decision": "approve", "note": "Must remain unavailable after plan cancellation."},
    )
    assert cancelled_warning_approval.status_code == 409

    # Responders receive an in-app notification and can see the verified incident
    # plus supporting reports, but cannot access the general report inbox.
    responder_notifications = client.get("/api/notifications", headers=responder_headers)
    assert responder_notifications.status_code == 200
    assert any(item["category"] == "incident" for item in responder_notifications.json())
    responder_incident = client.get(f"/api/incidents/{incident['id']}", headers=responder_headers)
    assert responder_incident.status_code == 200
    assert len(responder_incident.json()["reports"]) == 2
    assert client.get("/api/reports", headers=responder_headers).status_code == 403
    assert client.post(f"/api/reports/{report_id}/review", headers=responder_headers, json={"decision": "verify"}).status_code == 403

    # Responders can append operational updates without changing incident status/severity.
    activity_payloads = [
        {
            "activity_type": "situation_update",
            "message": "Water level is receding at the eastern access road; observation made at 09:15.",
            "location_text": "Eastern access road",
            "latitude": 23.023,
            "longitude": 72.572,
        },
        {
            "activity_type": "resource_request",
            "message": "Requesting two portable lighting units for the temporary inspection point.",
        },
        {
            "activity_type": "handover",
            "message": "Incoming shift briefed on access conditions and pending lighting request.",
        },
    ]
    created_activities = []
    for activity_payload in activity_payloads:
        response = client.post(
            f"/api/incidents/{incident['id']}/activity",
            headers=responder_headers,
            json=activity_payload,
        )
        assert response.status_code == 201, response.text
        created_activities.append(response.json())
        assert response.json()["actor_display_name"] == "Workflow Responder"
        assert response.json()["actor_role"] == "responder"
    assert created_activities[0]["latitude"] == 23.023
    refreshed_incident = client.get(f"/api/incidents/{incident['id']}", headers=responder_headers)
    assert refreshed_incident.json()["status"] == responder_incident.json()["status"]
    assert client.patch(
        f"/api/incidents/{incident['id']}", headers=responder_headers, json={"status": "resolved"}
    ).status_code == 403

    first_activity_page = client.get(
        f"/api/incidents/{incident['id']}/activity", headers=headers,
        params={"limit": 2, "offset": 0},
    )
    assert first_activity_page.status_code == 200, first_activity_page.text
    assert first_activity_page.json()["total"] == 3
    assert len(first_activity_page.json()["items"]) == 2
    assert first_activity_page.json()["has_more"] is True
    assert first_activity_page.json()["next_cursor"] is not None
    second_activity_page = client.get(
        f"/api/incidents/{incident['id']}/activity", headers=headers,
        params={"limit": 2, "offset": 2},
    )
    assert second_activity_page.status_code == 200
    assert len(second_activity_page.json()["items"]) == 1
    assert second_activity_page.json()["has_more"] is False
    first_ids = {row["id"] for row in first_activity_page.json()["items"]}
    second_ids = {row["id"] for row in second_activity_page.json()["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 3

    # Keyset pagination remains stable when a newer entry arrives between pages.
    late_activity = client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "observation", "message": "A newer note arrived while the first page was being read."},
    )
    assert late_activity.status_code == 201, late_activity.text
    cursor_activity_page = client.get(
        f"/api/incidents/{incident['id']}/activity", headers=headers,
        params={"limit": 2, **first_activity_page.json()["next_cursor"]},
    )
    assert cursor_activity_page.status_code == 200, cursor_activity_page.text
    assert [row["id"] for row in cursor_activity_page.json()["items"]] == [
        second_activity_page.json()["items"][0]["id"]
    ]
    assert late_activity.json()["id"] not in {row["id"] for row in cursor_activity_page.json()["items"]}
    assert cursor_activity_page.json()["has_more"] is False
    assert cursor_activity_page.json()["next_cursor"] is None
    assert cursor_activity_page.json()["total"] == 4
    assert client.get(
        f"/api/incidents/{incident['id']}/activity", headers=citizen_headers
    ).status_code == 403
    assert client.post(
        f"/api/incidents/{incident['id']}/activity", headers=citizen_headers,
        json=activity_payloads[0],
    ).status_code == 403
    assert client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "situation_update", "message": "Only one coordinate supplied.", "latitude": 23.0},
    ).status_code == 422
    assert client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "situation_update", "message": "          "},
    ).status_code == 422
    assert client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "dispatch_resource", "message": "This type is not supported."},
    ).status_code == 422
    assert client.get(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        params={"limit": 101},
    ).status_code == 422
    assert client.get(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        params={"before_id": created_activities[0]["id"]},
    ).status_code == 422
    assert client.get(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        params={"offset": 1, **first_activity_page.json()["next_cursor"]},
    ).status_code == 422
    assert client.get(
        "/api/incidents/not-a-real-incident/activity", headers=headers
    ).status_code == 404
    with api_main.SessionLocal() as activity_db:
        from app.db.models import AuditLog
        activity_audits = activity_db.query(AuditLog).filter_by(
            action="incident.activity_added", resource_id=incident["id"]
        ).all()
        assert len(activity_audits) == 4
        assert all("message" not in row.details for row in activity_audits)

    # Incident lifecycle is forward-only: verified -> monitoring -> resolved -> closed.
    monitoring = client.patch(f"/api/incidents/{incident['id']}", headers=headers, json={"status": "monitoring"})
    assert monitoring.status_code == 200, monitoring.text
    resolved = client.patch(f"/api/incidents/{incident['id']}", headers=headers, json={"status": "resolved"})
    assert resolved.status_code == 200, resolved.text
    post_resolution_note = client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "note", "message": "Post-resolution observation logged before formal closure."},
    )
    assert post_resolution_note.status_code == 201, post_resolution_note.text
    invalid_reopen = client.patch(f"/api/incidents/{incident['id']}", headers=headers, json={"status": "monitoring"})
    assert invalid_reopen.status_code == 409
    closed = client.patch(f"/api/incidents/{incident['id']}", headers=headers, json={"status": "closed"})
    assert closed.status_code == 200, closed.text
    # Idempotent retries return the original snapshot even after incident closure.
    replay_after_close = client.post(
        f"/api/incidents/{incident['id']}/response-plans", headers=plan_headers,
        json={"template_key": "flood_coordination_v1"},
    )
    assert replay_after_close.status_code == 200, replay_after_close.text
    assert replay_after_close.json()["id"] == plan["id"]
    assert replay_after_close.json()["idempotent_replay"] is True
    closed_edit = client.patch(f"/api/incidents/{incident['id']}", headers=headers, json={"summary": "Should not change after close"})
    assert closed_edit.status_code == 409
    closed_activity = client.post(
        f"/api/incidents/{incident['id']}/activity", headers=responder_headers,
        json={"activity_type": "note", "message": "This should be rejected for a closed incident."},
    )
    assert closed_activity.status_code == 409
    retained_activity = client.get(f"/api/incidents/{incident['id']}/activity", headers=responder_headers)
    assert retained_activity.status_code == 200
    assert retained_activity.json()["total"] == 5

    # Closed incidents cannot accept new supporting reports.
    closed_link_report = client.post("/api/reports", headers=headers, json={
        "title": "Synthetic follow-up for closed incident",
        "description": "Synthetic test record used to verify closed incident linking rules.",
        "disaster_type": "flood", "urgency": "low", "language": "en",
    })
    assert closed_link_report.status_code == 201, closed_link_report.text
    rejected_link = client.post(f"/api/reports/{closed_link_report.json()['id']}/review", headers=headers, json={
        "decision": "link", "incident_id": incident["id"],
    })
    assert rejected_link.status_code == 409

    # Rejected reports are terminal and cannot later become incidents.
    rejected_report = client.post("/api/reports", headers=headers, json={
        "title": "Synthetic report for rejection test",
        "description": "Synthetic test record used to verify analyst rejection is terminal.",
        "disaster_type": "other", "urgency": "unknown", "language": "en",
    })
    assert rejected_report.status_code == 201, rejected_report.text
    rejected_decision = client.post(f"/api/reports/{rejected_report.json()['id']}/review", headers=headers, json={
        "decision": "reject", "note": "Synthetic record is not a real incident.",
    })
    assert rejected_decision.status_code == 200, rejected_decision.text
    rejected_verify = client.post(f"/api/reports/{rejected_report.json()['id']}/review", headers=headers, json={"decision": "verify"})
    assert rejected_verify.status_code == 409
    rejected_link = client.post(f"/api/reports/{rejected_report.json()['id']}/review", headers=headers, json={
        "decision": "link", "incident_id": incident["id"],
    })
    assert rejected_link.status_code == 409

    # Anonymous submissions cannot be sent a direct request for more information.
    anonymous_report = client.post("/api/reports", json={
        "title": "Anonymous synthetic report",
        "description": "Anonymous synthetic report used to validate review constraints.",
        "disaster_type": "other", "urgency": "unknown", "language": "en",
    })
    assert anonymous_report.status_code == 201, anonymous_report.text
    anonymous_request = client.post(f"/api/reports/{anonymous_report.json()['id']}/review", headers=headers, json={
        "decision": "request_info", "note": "Please provide a verified contact method.",
    })
    assert anonymous_request.status_code == 409

    # Server-side report search must find anonymous submissions beyond the old
    # first-150 frontend window while preserving role isolation and stable pages.
    anonymous_search = client.get(
        "/api/reports/search",
        headers=headers,
        params={"q": anonymous_report.json()["tracking_code"], "page": 1, "page_size": 10},
    )
    assert anonymous_search.status_code == 200, anonymous_search.text
    assert anonymous_search.json()["total"] == 1
    assert anonymous_search.json()["items"][0]["id"] == anonymous_report.json()["id"]
    assert anonymous_search.json()["items"][0]["is_anonymous"] is True

    pagination_reports = []
    for number, urgency_value in ((1, "low"), (2, "moderate"), (3, "critical")):
        created = client.post("/api/reports", json={
            "title": f"Pagination QA report {number}",
            "description": "Synthetic pagination fixture; no actual emergency occurred.",
            "disaster_type": "other",
            "urgency": urgency_value,
            "language": "en",
        })
        assert created.status_code == 201, created.text
        pagination_reports.append(created.json())

    first_page = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination QA report", "source_type": "citizen", "disaster_type": "other",
        "status_filter": "submitted", "page": 1, "page_size": 2,
    })
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["total"] == 3
    assert first_page.json()["total_pages"] == 2
    assert first_page.json()["has_next"] is True
    assert len(first_page.json()["items"]) == 2

    second_page = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination QA report", "source_type": "citizen", "disaster_type": "other",
        "status_filter": "submitted", "page": 2, "page_size": 2,
    })
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["has_previous"] is True
    assert len(second_page.json()["items"]) == 1
    first_ids = {item["id"] for item in first_page.json()["items"]}
    second_ids = {item["id"] for item in second_page.json()["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 3

    inclusive_dates = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination QA report", "date_from": "2000-01-01", "date_to": "2100-01-01",
    })
    assert inclusive_dates.status_code == 200, inclusive_dates.text
    assert inclusive_dates.json()["total"] == 3
    literal_wildcard = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination%",
    })
    assert literal_wildcard.status_code == 200, literal_wildcard.text
    assert literal_wildcard.json()["total"] == 0

    urgency_page = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination QA report", "sort": "urgency", "page_size": 3,
    })
    assert urgency_page.status_code == 200, urgency_page.text
    assert [item["urgency"] for item in urgency_page.json()["items"]] == ["critical", "moderate", "low"]

    out_of_range = client.get("/api/reports/search", headers=headers, params={
        "q": "Pagination QA report", "page": 99, "page_size": 2,
    })
    assert out_of_range.status_code == 200
    assert out_of_range.json()["page"] == 2
    assert len(out_of_range.json()["items"]) == 1

    invalid_dates = client.get("/api/reports/search", headers=headers, params={
        "date_from": "2026-11-04", "date_to": "2026-11-03",
    })
    assert invalid_dates.status_code == 422

    citizen_private_search = client.get("/api/reports/search", headers=citizen_headers, params={
        "q": "Anonymous synthetic report",
    })
    assert citizen_private_search.status_code == 200
    assert citizen_private_search.json()["total"] == 0
    citizen_own_search = client.get("/api/reports/search", headers=citizen_headers, params={
        "q": "Road access blocked",
    })
    assert citizen_own_search.status_code == 200
    assert citizen_own_search.json()["total"] == 1
    assert citizen_own_search.json()["items"][0]["id"] == citizen_report_id
    assert client.get("/api/reports/search", headers=responder_headers).status_code == 403

    audit = client.get("/api/audit", headers=headers)
    assert audit.status_code == 200
    actions = {row["action"] for row in audit.json()}
    assert "report.verify" in actions
    assert "report.link" in actions

    # Citizens cannot access incident intelligence or reports belonging to others.
    assert client.get("/api/incidents", headers=citizen_headers).status_code == 403
    # Evidence integrity scans are asynchronous and administrator-only.
    class FakeQueuedJob:
        id = "evidence-integrity-test-job"

    enqueued = {}

    def fake_enqueue(function_path, *args):
        enqueued["function_path"] = function_path
        enqueued["args"] = args
        return FakeQueuedJob()

    monkeypatch.setattr(api_main, "enqueue_job", fake_enqueue)
    started_audit = client.post("/api/admin/evidence-integrity-audits", headers=headers)
    assert started_audit.status_code == 202, started_audit.text
    started_payload = started_audit.json()
    audit_run_id = started_payload["run_id"]
    assert started_payload["status"] == "queued"
    assert enqueued["function_path"] == "app.worker.run_evidence_integrity_audit"
    assert enqueued["args"] == (audit_run_id,)

    queued_status = client.get(
        f"/api/admin/evidence-integrity-audits/{audit_run_id}", headers=headers
    )
    assert queued_status.status_code == 200, queued_status.text
    assert queued_status.json()["status"] == "queued"
    assert queued_status.json()["job_id"] == FakeQueuedJob.id
    assert client.post("/api/admin/evidence-integrity-audits", headers=responder_headers).status_code == 403
    assert client.post("/api/admin/evidence-integrity-audits", headers=citizen_headers).status_code == 403
    assert client.get(
        f"/api/admin/evidence-integrity-audits/{audit_run_id}", headers=responder_headers
    ).status_code == 403
    assert client.get(
        f"/api/admin/evidence-integrity-audits/{audit_run_id}", headers=citizen_headers
    ).status_code == 403

    from app.db.models import AuditLog
    with api_main.SessionLocal() as audit_db:
        audit_db.add(AuditLog(
            id=str(uuid4()), actor_user_id=None, action="evidence.integrity_audit_completed",
            resource_type="evidence_integrity_audit", resource_id=audit_run_id,
            details={"run_id": audit_run_id, "status": "completed", "checked": 7, "passed": 7, "failed": 0},
        ))
        audit_db.commit()
    completed_status = client.get(
        f"/api/admin/evidence-integrity-audits/{audit_run_id}", headers=headers
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["status"] == "completed"
    assert completed_status.json()["summary"]["checked"] == 7
    assert client.get("/api/admin/evidence-integrity-audits/not-a-uuid", headers=headers).status_code == 422
    assert client.get(
        f"/api/admin/evidence-integrity-audits/{uuid4()}", headers=headers
    ).status_code == 404

    failed_run_id = str(uuid4())
    with api_main.SessionLocal() as audit_db:
        audit_db.add(AuditLog(
            id=str(uuid4()), actor_user_id=None, action="evidence.integrity_audit_failed",
            resource_type="evidence_integrity_audit", resource_id=failed_run_id,
            details={"run_id": failed_run_id, "error_type": "StorageUnavailable"},
        ))
        audit_db.commit()
    failed_status = client.get(
        f"/api/admin/evidence-integrity-audits/{failed_run_id}", headers=headers
    )
    assert failed_status.status_code == 200, failed_status.text
    assert failed_status.json()["status"] == "failed"
    assert failed_status.json()["error_type"] == "StorageUnavailable"

    monkeypatch.setattr(api_main, "enqueue_job", lambda *_args, **_kwargs: None)
    unavailable_audit = client.post("/api/admin/evidence-integrity-audits", headers=headers)
    assert unavailable_audit.status_code == 503




def test_research_dataset_label_review_seal_and_immutable_export(client):
    import hashlib
    import json

    from app.db.models import EventPairLabel, Report, User
    from app.db.session import SessionLocal
    from app.services.research_datasets import pair_fingerprint, report_content_fingerprint

    with SessionLocal() as db:
        admin = db.query(User).filter_by(email="workflow-admin@cignal-test.com").one()
        token = api_main._token_for_user(admin)["access_token"]
        admin_id = admin.id
    headers = {"Authorization": f"Bearer {token}"}

    dataset_response = client.post("/api/research/datasets", headers=headers, json={
        "slug": "api-governed-event-pairs",
        "name": "API governed event pairs",
        "description": "Reviewed event-pair dataset for supervised event matching.",
        "purpose": "Train and evaluate event-pair matching on reviewed disaster observations.",
        "license_identifier": "CC-BY-4.0",
        "license_url": "https://example.org/terms",
        "legal_basis": "official_open_data",
        "consent_scope": "open_data_terms",
        "source_types": ["usgs", "gdacs", "imd"],
    })
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["id"]

    pairs = []
    with SessionLocal() as db:
        for index in range(16):
            group = f"api-disaster-{index // 4 + 1}"
            source_a = "usgs" if index % 2 == 0 else "gdacs"
            source_b = "gdacs" if source_a == "usgs" else "imd"
            description_a = (
                f"Distinct flood observation {index} near an affected road and homes. "
                "Residents report rising water and blocked local access."
            )
            description_b = (
                f"Independent flood observation counterpart {index} at the river-side area. "
                "Water levels are increasing and response access is constrained."
            )
            if index == 0:
                description_a += " Contact: flooddesk@example.org"
            if index == 1:
                description_b += " Phone: 9876543210"
            first = Report(
                id=str(uuid4()), tracking_code=f"CIG-DATA-A-{index:03d}",
                title=f"Flood observation A {index}", description=description_a,
                disaster_type="flood", urgency="moderate", status="analyzed",
                latitude=23.0, longitude=72.5, language="en",
                source_type=source_a, source_name=source_a.upper(),
                source_record_id=f"data-engine-a-{index}", provenance="external",
            )
            second = Report(
                id=str(uuid4()), tracking_code=f"CIG-DATA-B-{index:03d}",
                title=f"Flood observation B {index}", description=description_b,
                disaster_type="flood", urgency="moderate", status="analyzed",
                latitude=23.01, longitude=72.51, language="en",
                source_type=source_b, source_name=source_b.upper(),
                source_record_id=f"data-engine-b-{index}", provenance="external",
            )
            if index == 0:
                first.latitude = 23.123456
                first.longitude = 72.987654
            db.add_all([first, second])
            pairs.append((group, first, second, "same_event" if index % 2 == 0 else "different_event"))
        db.commit()

    first_group, first_a, first_b, first_label = pairs[0]
    label_response = client.post(
        f"/api/research/datasets/{dataset_id}/pair-labels",
        headers=headers,
        json={
            "report_a_id": first_a.id,
            "report_b_id": first_b.id,
            "label": first_label,
            "split_group": first_group,
            "rationale": "Two independent sources describe this event pair.",
        },
    )
    assert label_response.status_code == 201, label_response.text
    label_id = label_response.json()["id"]
    review_response = client.patch(
        f"/api/research/datasets/{dataset_id}/pair-labels/{label_id}/review",
        headers=headers,
        json={"decision": "approve", "privacy_review": "passed", "note": "PII screen reviewed."},
    )
    assert review_response.status_code == 200, review_response.text

    with SessionLocal() as db:
        for group, report_a, report_b, label_value in pairs[1:]:
            db.add(EventPairLabel(
                id=str(uuid4()), dataset_id=dataset_id,
                report_a_id=report_a.id, report_b_id=report_b.id,
                pair_fingerprint=pair_fingerprint(report_a.id, report_b.id),
                label=label_value, split_group=group,
                rationale="Reviewed synthetic integration fixture.",
                label_method="analyst_review", review_status="approved",
                privacy_review_status="passed",
                provenance_snapshot={
                    "report_a": {
                        "report_id": report_a.id,
                        "content_sha256": report_content_fingerprint(report_a),
                        "source_type": report_a.source_type,
                        "source_name": report_a.source_name,
                    },
                    "report_b": {
                        "report_id": report_b.id,
                        "content_sha256": report_content_fingerprint(report_b),
                        "source_type": report_b.source_type,
                        "source_name": report_b.source_name,
                    },
                },
                created_by=admin_id, reviewed_by=admin_id,
            ))
        db.commit()

    version_response = client.post(
        f"/api/research/datasets/{dataset_id}/versions", headers=headers, json={}
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    assert version["record_count"] == 16
    assert version["split_group_count"] == 4
    assert version["state"] == "sealed"
    assert version["manifest"]["governance"]["coordinate_precision"] == 2

    export_url = (
        f"/api/research/datasets/{dataset_id}/versions/{version['id']}/export"
    )
    exported = client.get(export_url, headers=headers)
    assert exported.status_code == 200, exported.text
    assert hashlib.sha256(exported.content).hexdigest() == version["content_sha256"]
    decoded = exported.content.decode("utf-8")
    assert "flooddesk@example.org" not in decoded
    assert "9876543210" not in decoded
    rows = [json.loads(line) for line in decoded.splitlines()]
    assert len(rows) == 16
    assert {row["same_event"] for row in rows} == {0, 1}
    target_pair = pair_fingerprint(first_a.id, first_b.id)
    target_row = next(row for row in rows if row["pair_fingerprint"] == target_pair)
    coordinates = {
        (target_row["latitude_a"], target_row["longitude_a"]),
        (target_row["latitude_b"], target_row["longitude_b"]),
    }
    assert (23.12, 72.99) in coordinates

    # Sealed items are immutable even if a source report is edited later.
    original_payload = exported.content
    with SessionLocal() as db:
        changed = db.get(Report, first_a.id)
        changed.description = "Source report content changed after dataset sealing."
        db.commit()
    exported_again = client.get(export_url, headers=headers)
    assert exported_again.content == original_payload


def test_research_dataset_api_routes_are_registered():
    from app.api.main import app

    paths = set(app.openapi().get("paths", {}))
    assert {
        "/api/research/datasets",
        "/api/research/datasets/{dataset_id}",
        "/api/research/datasets/{dataset_id}/pair-labels",
        "/api/research/datasets/{dataset_id}/pair-labels/{label_id}/review",
        "/api/research/datasets/{dataset_id}/versions",
        "/api/research/datasets/{dataset_id}/versions/{version_id}/export",
        "/api/intelligence/event-replays",
        "/api/intelligence/event-replays/{run_id}",
        "/api/intelligence/event-replays/{run_id}/results",
    }.issubset(paths)
