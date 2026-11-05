from __future__ import annotations

import pytest

from app.core.config import settings
from app.storage.local import LocalEvidenceStorage


def test_local_evidence_storage_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    storage = LocalEvidenceStorage()
    key = "reports/report-1/evidence.bin"
    payload = b"sample-evidence-bytes"
    assert storage.save(key, payload, "application/octet-stream") == key
    assert storage.read(key) == payload
    storage.delete(key)
    assert not (tmp_path / key).exists()


def test_local_storage_blocks_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    storage = LocalEvidenceStorage()
    with pytest.raises(ValueError):
        storage.save("../../etc/passwd", b"x", "text/plain")
    with pytest.raises(ValueError):
        storage.read("../outside")
