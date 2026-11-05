from app.core.config import settings
from app.storage.base import EvidenceStorage


class S3EvidenceStorage(EvidenceStorage):
    def __init__(self):
        import boto3
        self.client = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
        )
        self.bucket = settings.s3_bucket

    def save(self, key: str, content: bytes, content_type: str) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type, ServerSideEncryption="AES256")
        return key

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
