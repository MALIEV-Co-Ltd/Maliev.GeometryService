import io
import logging
from typing import Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_LOCAL_UPLOAD_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MOCK_STORAGE_PATH_PREFIX = "/upload/v1/mock-storage/"


class PermanentDownloadError(Exception):
    """Exception raised for non-retryable download errors (e.g. 404, 401)."""


class IStorageService(Protocol):
    async def download_file(self, url: str) -> io.BytesIO: ...


def normalize_download_url(url: str) -> str:
    """Rewrite local UploadService mock storage URLs for container access."""
    parsed_url = urlparse(url)
    if (
        parsed_url.path.startswith(_MOCK_STORAGE_PATH_PREFIX)
        and parsed_url.hostname in _LOCAL_UPLOAD_HOSTS
    ):
        upload_service_url = urlparse(settings.UPLOAD_SERVICE_URL)
        if upload_service_url.scheme and upload_service_url.netloc:
            return urlunparse(
                (
                    upload_service_url.scheme,
                    upload_service_url.netloc,
                    parsed_url.path,
                    "",
                    parsed_url.query,
                    "",
                )
            )

    return url


class HttpDownloadService:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=60.0)

    async def download_file(self, url: str) -> io.BytesIO:
        download_url = normalize_download_url(url)
        try:
            response = await self.client.get(download_url)
            response.raise_for_status()
            return io.BytesIO(response.content)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in [401, 403, 404]:
                raise PermanentDownloadError(
                    "Permanent failure downloading "
                    f"{download_url}: {e.response.status_code}"
                ) from e
            raise

    async def close(self) -> None:
        await self.client.aclose()
