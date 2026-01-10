import io
from typing import Protocol

import httpx


class PermanentDownloadError(Exception):
    """Exception raised for non-retryable download errors (e.g. 404, 401)."""


class IStorageService(Protocol):
    async def download_file(self, url: str) -> io.BytesIO: ...


class HttpDownloadService:
    """Downloads files from a provided URL (e.g., GCS Signed URL)."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=60.0)

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

    async def close(self) -> None:
        await self.client.aclose()
