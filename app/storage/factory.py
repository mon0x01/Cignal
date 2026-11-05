from app.core.config import settings
from app.storage.local import LocalEvidenceStorage


def get_storage():
    if settings.storage_backend.lower() == "s3":
        from app.storage.s3 import S3EvidenceStorage
        return S3EvidenceStorage()
    return LocalEvidenceStorage()
