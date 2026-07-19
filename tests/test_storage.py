import httpx
import pytest
import respx

from src.infrastructure.storage import (
    HttpDownloadService,
    PermanentDownloadError,
    normalize_download_url,
)


def test_normalize_download_url_rewrites_local_mock_storage_url(monkeypatch):
    monkeypatch.setattr(
        "src.infrastructure.storage.settings.UPLOAD_SERVICE_URL",
        "http://aspire.dev.internal:45389",
    )

    result = normalize_download_url(
        "http://localhost:55333/upload/v1/mock-storage/token-123?download=1"
    )

    assert (
        result
        == "http://aspire.dev.internal:45389/upload/v1/mock-storage/token-123?download=1"
    )


def test_normalize_download_url_keeps_non_mock_urls(monkeypatch):
    monkeypatch.setattr(
        "src.infrastructure.storage.settings.UPLOAD_SERVICE_URL",
        "http://aspire.dev.internal:45389",
    )
    url = "https://storage.googleapis.com/bucket/file.stl?signature=abc"

    assert normalize_download_url(url) == url


@pytest.mark.asyncio
async def test_download_file_success():
    service = HttpDownloadService()
    url = "https://example.com/file.stl"
    content = b"test content"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=content))
        result = await service.download_file(url)
        assert result.getvalue() == content

    await service.close()


@pytest.mark.asyncio
async def test_download_file_uses_normalized_mock_storage_url(monkeypatch):
    monkeypatch.setattr(
        "src.infrastructure.storage.settings.UPLOAD_SERVICE_URL",
        "http://aspire.dev.internal:45389",
    )
    service = HttpDownloadService()
    original_url = "http://localhost:55333/upload/v1/mock-storage/token-123"
    normalized_url = "http://aspire.dev.internal:45389/upload/v1/mock-storage/token-123"
    content = b"test content"

    with respx.mock:
        respx.get(normalized_url).mock(
            return_value=httpx.Response(200, content=content)
        )
        result = await service.download_file(original_url)
        assert result.getvalue() == content

    await service.close()


@pytest.mark.asyncio
async def test_download_file_permanent_error():
    service = HttpDownloadService()
    url = "https://example.com/nonexistent"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(PermanentDownloadError):
            await service.download_file(url)

    await service.close()


@pytest.mark.asyncio
async def test_download_file_transient_error():
    service = HttpDownloadService()
    url = "https://example.com/error"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await service.download_file(url)

    await service.close()
