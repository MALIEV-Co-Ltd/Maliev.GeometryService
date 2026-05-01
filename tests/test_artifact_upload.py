"""
Tests for the upload_artifact method in UploadConsumer.
Verifies that artifacts are uploaded via UploadService with a Bearer token
and that errors are handled gracefully.
"""

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from src.consumers.upload_consumer import UploadConsumer

# ── Fixtures ─────────────────────────────────────────────────────────────────

FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.test.signature"
UPLOAD_URL = "http://localhost:6900"
ARTIFACT_PATH = "projects/abc/model.stl_thumb.png"
ARTIFACT_DATA = b"fake-png-bytes"
PARENT_UPLOAD_ID = "upload-uuid-1234"


@pytest.fixture
def consumer(monkeypatch: pytest.MonkeyPatch) -> UploadConsumer:
    monkeypatch.setenv("UPLOAD_SERVICE_URL", UPLOAD_URL)
    monkeypatch.setenv("JWT_SECURITY_KEY", "test-key-32-chars-padding-here!!")
    monkeypatch.setenv("JWT_ISSUER", "https://api.maliev.com")
    monkeypatch.setenv("JWT_AUDIENCE", "https://api.maliev.com")

    import importlib

    import src.core.config as cfg

    importlib.reload(cfg)

    storage = AsyncMock()
    processor = AsyncMock()
    c = UploadConsumer(storage, processor)
    # Patch token provider to return a known token
    c._token_provider = MagicMock()
    c._token_provider.get_token.return_value = FAKE_TOKEN
    return c


# ── Happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_artifact_sends_bearer_token(
    consumer: UploadConsumer,
) -> None:
    """upload_artifact attaches Authorization: Bearer <token> to the request."""
    captured_headers: dict[str, str] = {}

    with respx.mock:
        route = respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(
                200, json={"storagePath": ARTIFACT_PATH, "downloadUrl": "http://x"}
            )
        )
        await consumer.upload_artifact(
            ARTIFACT_DATA, ARTIFACT_PATH, "image/png", PARENT_UPLOAD_ID
        )
        captured_headers = dict(route.calls[0].request.headers)

    assert captured_headers.get("authorization") == f"Bearer {FAKE_TOKEN}"


@pytest.mark.asyncio
async def test_upload_artifact_sends_correct_payload(
    consumer: UploadConsumer,
) -> None:
    """upload_artifact sends the correct JSON body to UploadService."""
    import json

    captured_body: dict = {}

    with respx.mock:
        route = respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(
                200, json={"storagePath": ARTIFACT_PATH, "downloadUrl": "http://x"}
            )
        )
        await consumer.upload_artifact(
            ARTIFACT_DATA, ARTIFACT_PATH, "image/png", PARENT_UPLOAD_ID
        )
        captured_body = json.loads(route.calls[0].request.content)

    assert captured_body["storagePath"] == ARTIFACT_PATH
    assert captured_body["contentType"] == "image/png"
    assert captured_body["parentUploadId"] == PARENT_UPLOAD_ID
    assert base64.b64decode(captured_body["artifactData"]) == ARTIFACT_DATA
    # artifactId should be a valid UUID string
    import uuid

    uuid.UUID(captured_body["artifactId"])  # raises if invalid


@pytest.mark.asyncio
async def test_upload_artifact_requests_token_from_provider(
    consumer: UploadConsumer,
) -> None:
    """upload_artifact calls get_token() on the token provider exactly once per call."""
    with respx.mock:
        respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(
                200, json={"storagePath": ARTIFACT_PATH, "downloadUrl": "http://x"}
            )
        )
        await consumer.upload_artifact(ARTIFACT_DATA, ARTIFACT_PATH, "image/png")

    assert consumer._token_provider.get_token.call_count == 1


# ── Error handling ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_artifact_does_not_raise_on_401(
    consumer: UploadConsumer,
) -> None:
    """401 from UploadService logs a warning but does not raise."""
    with respx.mock:
        respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(401)
        )
        # Should not raise
        await consumer.upload_artifact(ARTIFACT_DATA, ARTIFACT_PATH, "image/png")


@pytest.mark.asyncio
async def test_upload_artifact_does_not_raise_on_500(
    consumer: UploadConsumer,
) -> None:
    """5xx from UploadService logs an error but does not raise."""
    with respx.mock:
        respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(500)
        )
        await consumer.upload_artifact(ARTIFACT_DATA, ARTIFACT_PATH, "image/png")


@pytest.mark.asyncio
async def test_upload_artifact_does_not_raise_on_connection_error(
    consumer: UploadConsumer,
) -> None:
    """Network error logs an error but does not raise — processing continues."""
    with respx.mock:
        respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        await consumer.upload_artifact(ARTIFACT_DATA, ARTIFACT_PATH, "image/png")


@pytest.mark.asyncio
async def test_upload_artifact_empty_parent_upload_id(
    consumer: UploadConsumer,
) -> None:
    """upload_artifact works when parent_upload_id defaults to empty string."""
    with respx.mock:
        route = respx.post(f"{UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
            return_value=httpx.Response(
                200, json={"storagePath": ARTIFACT_PATH, "downloadUrl": "http://x"}
            )
        )
        await consumer.upload_artifact(ARTIFACT_DATA, ARTIFACT_PATH, "image/png")
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["parentUploadId"] == ""
