import httpx
import pytest
import respx

from src.infrastructure.storage import HttpDownloadService, PermanentDownloadError


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
