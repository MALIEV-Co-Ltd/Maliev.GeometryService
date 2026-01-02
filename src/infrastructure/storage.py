import io
from typing import Protocol

import httpx


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
        response = await self.client.get(url)
        response.raise_for_status()
        return io.BytesIO(response.content)

    async def close(self) -> None:
        await self.client.aclose()
