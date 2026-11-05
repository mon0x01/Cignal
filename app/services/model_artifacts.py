"""Safe creation of portable archives for Cignal's trusted local ML artifacts."""
from __future__ import annotations

import tarfile
from pathlib import Path


def archive_model_directory(model_root: str | Path, archive_path: str | Path) -> list[str]:
    """Archive regular files under model_root; do not follow symlinks or include outside paths."""
    root = Path(model_root)
    destination = Path(archive_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with tarfile.open(destination, "w:gz") as archive:
        if not root.is_dir():
            return included
        resolved_root = root.resolve()
        for item in sorted(root.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            resolved_item = item.resolve(strict=True)
            try:
                relative = resolved_item.relative_to(resolved_root)
            except ValueError:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                continue
            name = relative.as_posix()
            archive.add(resolved_item, arcname=name, recursive=False)
            included.append(name)
    return included
