#!/usr/bin/env python3
"""Export a sealed research dataset version into the shared model volume."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.db.models import ResearchDataset, ResearchDatasetVersion, ResearchDatasetVersionItem
from app.db.session import SessionLocal
from app.services.research_datasets import serialize_dataset_items


def resolve_output_path(value: str, model_root: Path) -> Path:
    root = model_root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Output must remain inside the shared /data/models volume")
    if candidate.suffix.lower() != ".jsonl":
        raise ValueError("Output filename must end in .jsonl")
    return candidate


def _publish_immutable(path: Path, payload: bytes) -> bool:
    """Atomically publish a file without ever overwriting an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return False
        raise FileExistsError(f"Refusing to overwrite a different existing artifact: {path}")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        try:
            # Hard-link creation is atomic and fails rather than replacing a raced-in file.
            os.link(temporary, path)
            return True
        except FileExistsError as exc:
            if path.read_bytes() == payload:
                return False
            raise FileExistsError(
                f"Refusing to overwrite a different concurrently-created artifact: {path}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def export_version(dataset_id: str, version_id: str, output_path: Path) -> dict:
    dataset_id = str(UUID(dataset_id))
    version_id = str(UUID(version_id))
    with SessionLocal() as db:
        dataset = db.get(ResearchDataset, dataset_id)
        version = db.query(ResearchDatasetVersion).filter_by(
            id=version_id, dataset_id=dataset_id
        ).first()
        if dataset is None or version is None:
            raise ValueError("Dataset version not found")
        if version.state != "sealed":
            raise ValueError("Only sealed dataset versions can be exported")
        items = (
            db.query(ResearchDatasetVersionItem)
            .filter_by(version_id=version.id)
            .order_by(ResearchDatasetVersionItem.ordinal.asc())
            .all()
        )
        expected_ordinals = list(range(1, version.record_count + 1))
        actual_ordinals = [item.ordinal for item in items]
        if len(items) != version.record_count or actual_ordinals != expected_ordinals:
            raise ValueError("Stored dataset record count or ordinal sequence is inconsistent")
        payload = serialize_dataset_items(items)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != version.content_sha256:
            raise ValueError("Stored dataset content does not match its sealed SHA-256")

        manifest_payload = {
            "manifest_schema": "cignal_research_dataset_manifest_v1",
            "dataset_id": dataset.id,
            "dataset_slug": dataset.slug,
            "version_id": version.id,
            "version_number": version.version_number,
            "state": version.state,
            "record_count": version.record_count,
            "content_sha256": digest,
            "schema_version": version.schema_version,
            "manifest": version.manifest,
        }
        manifest_bytes = (
            json.dumps(
                manifest_payload, sort_keys=True, ensure_ascii=False, indent=2
            ) + "\n"
        ).encode()
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")

        # Preflight both paths before publishing either file.
        for path, content in ((output_path, payload), (manifest_path, manifest_bytes)):
            if path.exists() and path.read_bytes() != content:
                raise FileExistsError(f"Refusing to overwrite a different existing artifact: {path}")

        data_created = _publish_immutable(output_path, payload)
        manifest_created = _publish_immutable(manifest_path, manifest_bytes)
        return {
            "dataset_slug": dataset.slug,
            "version_number": version.version_number,
            "record_count": version.record_count,
            "content_sha256": digest,
            "output_path": str(output_path),
            "manifest_path": str(manifest_path),
            "bytes": len(payload),
            "data_created": data_created,
            "manifest_created": manifest_created,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_id")
    parser.add_argument("version_id")
    parser.add_argument("--output", required=True, help="Output path inside /data/models")
    args = parser.parse_args()
    try:
        output = resolve_output_path(args.output, settings.event_pair_model_path.parent)
        result = export_version(args.dataset_id, args.version_id, output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
