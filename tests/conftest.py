"""Shared test configuration; all tests use isolated, disposable local paths."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = ROOT / "tests" / ".cignal-test.sqlite3"

# Set environment before importing application modules (Settings is a singleton).
os.environ["APP_ENV"] = "test"
os.environ["DEMO_MODE"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["APP_SECRET_KEY"] = "test-app-secret-key-with-at-least-thirty-two-characters"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-with-at-least-thirty-two-characters"
os.environ["EVIDENCE_DIR"] = str(ROOT / "tests" / ".evidence")
os.environ["BACKUP_DIR"] = str(ROOT / "tests" / ".backup")
os.environ["MFA_REQUIRED"] = "true"
os.environ["RATE_LIMIT_FAIL_CLOSED"] = "false"
os.environ["AI_ENABLE_SKLEARN"] = "false"
os.environ["AI_ENABLE_PRETRAINED_NLP"] = "false"

TEST_DB.unlink(missing_ok=True)


def pytest_sessionfinish(session, exitstatus):
    """Remove ephemeral test artifacts after all tests finish."""
    for path in (TEST_DB, ROOT / "tests" / ".evidence", ROOT / "tests" / ".backup"):
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
