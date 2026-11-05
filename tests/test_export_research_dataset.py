from __future__ import annotations

import stat

import pytest

from scripts.export_research_dataset import _publish_immutable, resolve_output_path


def test_resolve_output_path_keeps_relative_export_inside_model_volume(tmp_path):
    root = tmp_path / "models"
    path = resolve_output_path("datasets/observations.jsonl", root)
    assert path == root / "datasets" / "observations.jsonl"


def test_resolve_output_path_rejects_traversal_and_non_jsonl(tmp_path):
    root = tmp_path / "models"
    with pytest.raises(ValueError, match="inside"):
        resolve_output_path("../../outside.jsonl", root)
    with pytest.raises(ValueError, match="jsonl"):
        resolve_output_path("datasets/observations.csv", root)


def test_publish_immutable_file_is_atomic_idempotent_and_non_overwriting(tmp_path):
    path = tmp_path / "models" / "dataset.jsonl"
    assert _publish_immutable(path, b'{"same_event":1}\n') is True
    assert path.read_bytes() == b'{"same_event":1}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert _publish_immutable(path, b'{"same_event":1}\n') is False
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        _publish_immutable(path, b'{"same_event":0}\n')
    assert path.read_bytes() == b'{"same_event":1}\n'
