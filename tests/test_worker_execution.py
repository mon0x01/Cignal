from __future__ import annotations

from contextlib import nullcontext

import pytest


def _claim():
    return {
        "status": "claimed",
        "outbox_id": "outbox-1",
        "job_id": "logical-1",
        "function_path": "app.worker.process_report",
        "task_args": ["report-1", 3],
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "lease_token": "lease-1",
    }


def _load_worker(monkeypatch, *, acquired=True):
    import app.services.job_execution as execution
    import app.worker as worker

    disposed = []
    monkeypatch.setattr(worker.engine, "dispose", lambda **kwargs: disposed.append(kwargs))
    monkeypatch.setattr(worker, "_outbox_execution_lock", lambda _outbox_id: nullcontext(acquired))
    monkeypatch.setattr(worker, "_outbox_timeout_seconds", lambda _outbox_id: 60)
    monkeypatch.setattr(execution, "claim_execution", lambda *args, **kwargs: _claim())
    return worker, execution, disposed


def test_worker_wrapper_executes_allowlisted_target_and_records_result(monkeypatch):
    worker, execution, disposed = _load_worker(monkeypatch)
    target_calls = []
    completions = []
    monkeypatch.setattr(worker, "process_report", lambda *args: target_calls.append(args) or {"ok": True})
    monkeypatch.setattr(
        execution, "complete_execution",
        lambda *args, **kwargs: completions.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        execution, "fail_execution",
        lambda *args, **kwargs: pytest.fail("successful execution must not be failed"),
    )

    result = worker.execute_outbox_job("outbox-1", "rq-delivery-1")

    assert result["status"] == "completed"
    assert len(result["result_sha256"]) == 64
    assert target_calls == [("report-1", 3)]
    assert len(completions) == 1
    assert disposed == [{"close": False}]


def test_worker_wrapper_records_failure_and_reraises(monkeypatch):
    worker, execution, _ = _load_worker(monkeypatch)
    failures = []

    def fail_target(*_args):
        raise RuntimeError("synthetic target failure")

    monkeypatch.setattr(worker, "process_report", fail_target)
    monkeypatch.setattr(
        execution, "fail_execution",
        lambda *args, **kwargs: failures.append((args, kwargs)) or "retried",
    )

    with pytest.raises(RuntimeError, match="synthetic target failure"):
        worker.execute_outbox_job("outbox-1", "rq-delivery-1")

    assert len(failures) == 1
    assert failures[0][0][:3] == ("outbox-1", "attempt-1", "lease-1")


def test_worker_wrapper_rejects_unallowlisted_function(monkeypatch):
    worker, execution, _ = _load_worker(monkeypatch)
    claim = _claim()
    claim["function_path"] = "os.system"
    monkeypatch.setattr(execution, "claim_execution", lambda *args, **kwargs: claim)
    failures = []
    monkeypatch.setattr(
        execution, "fail_execution",
        lambda *args, **kwargs: failures.append(args) or "dead",
    )

    with pytest.raises(ValueError, match="Unsupported durable background function"):
        worker.execute_outbox_job("outbox-1", "rq-delivery-1")

    assert len(failures) == 1


def test_worker_wrapper_defers_duplicate_when_database_fence_is_held(monkeypatch):
    worker, execution, disposed = _load_worker(monkeypatch, acquired=False)
    monkeypatch.setattr(
        execution, "claim_execution",
        lambda *args, **kwargs: pytest.fail("execution claim must not occur without the fence"),
    )

    with pytest.raises(RuntimeError, match="outbox_execution_fence_busy"):
        worker.execute_outbox_job("outbox-1", "rq-duplicate")

    assert disposed == [{"close": False}]
