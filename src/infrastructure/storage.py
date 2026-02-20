import io
import logging
from typing import Protocol

import boto3
import httpx
from botocore.exceptions import ClientError

from src.core.config import settings

logger = logging.getLogger(__name__)


class PermanentDownloadError(Exception):
    """Exception raised for non-retryable download errors (e.g. 404, 401)."""


class IStorageService(Protocol):
    async def download_file(self, url: str) -> io.BytesIO: ...
    async def upload_file(
        self, file_data: bytes, object_name: str, content_type: str
    ) -> None: ...


class HttpDownloadService:
    """Downloads files from a provided URL (e.g., GCS Signed URL)."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=60.0)
        # Initialize S3 client for uploads
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=f"http://{settings.STORAGE_ENDPOINT}",
            aws_access_key_id=settings.STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY,
        )

    async def download_file(self, url: str) -> io.BytesIO:
        """
        Downloads a file from a URL.
        """
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return io.BytesIO(response.content)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in [401, 403, 404]:
                raise PermanentDownloadError(
                    f"Permanent failure downloading {url}: {e.response.status_code}"
                ) from e
            raise

    async def upload_file(
        self, file_data: bytes, object_name: str, content_type: str
    ) -> None:
        """
        Uploads a file to S3/MinIO.
        """
        try:
            self.s3_client.put_object(
                Bucket=settings.STORAGE_BUCKET,
                Key=object_name,
                Body=file_data,
                ContentType=content_type,
            )
            logger.info(f"Uploaded {object_name} to bucket {settings.STORAGE_BUCKET}")
        except ClientError as e:
            logger.error(f"Failed to upload {object_name}: {e}")
            raise

    async def close(self) -> None:
        await self.client.aclose()
        # boto3 client doesn't need explicit close usually, but if it did:
        # self.s3_client.close()
