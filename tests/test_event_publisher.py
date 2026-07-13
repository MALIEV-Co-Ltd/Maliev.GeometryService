import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import pytest

from src.core.schemas import (
    DfmAnalysisReadyEvent,
    FileAnalysisFailedEvent,
    FileAnalyzedEvent,
    FileMetricsReadyEvent,
    PreviewImagesGeneratedEvent,
    SmallThumbnailReadyEvent,
)
from src.infrastructure.event_publisher import (
    initialize_event_publisher,
    publish_event,
)


class FakeExchange:
    def __init__(self) -> None:
        self.publications: list[tuple[aio_pika.Message, str]] = []

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        self.publications.append((message, routing_key))


METRICS = {
    "volumeCm3": 12.5,
    "supportVolumeCm3": 1.25,
    "surfaceAreaCm2": 42.0,
    "boundingBox": {"x": 10.0, "y": 20.0, "z": 30.0},
    "isManifold": True,
    "triangleCount": 1200,
    "eulerNumber": 2,
}

EVENT_CASES: list[tuple[type[Any], str, str, dict[str, Any], str, Any]] = [
    (
        FileAnalyzedEvent,
        "FileAnalyzedEvent",
        "maliev.geometryservice.v1.analysis.completed",
        {
            "fileId": "file-123",
            "metrics": METRICS,
            "processedAt": "2026-07-13T10:00:00Z",
        },
        "fileId",
        "file-123",
    ),
    (
        FileAnalysisFailedEvent,
        "FileAnalysisFailedEvent",
        "maliev.geometryservice.v1.analysis.failed",
        {
            "fileId": "file-123",
            "storagePath": "uploads/file-123.step",
            "errorCode": "FILE_CORRUPT",
            "details": "Invalid STEP data",
        },
        "errorCode",
        "FILE_CORRUPT",
    ),
    (
        FileMetricsReadyEvent,
        "FileMetricsReadyEvent",
        "maliev.geometryservice.v1.metrics.ready",
        {
            "fileId": "file-123",
            "storagePath": "uploads/file-123.step",
            "metrics": METRICS,
            "processedAt": "2026-07-13T10:00:00Z",
        },
        "storagePath",
        "uploads/file-123.step",
    ),
    (
        PreviewImagesGeneratedEvent,
        "PreviewImagesGeneratedEvent",
        "maliev.geometryservice.v1.preview-images.generated",
        {
            "fileId": "file-123",
            "storagePath": "uploads/file-123.step",
            "previewImages": {"thumbnailSmall": "previews/file-123-small.webp"},
            "generatedAt": "2026-07-13T10:00:00Z",
        },
        "previewImages",
        {
            "frontSmall": None,
            "backSmall": None,
            "leftSmall": None,
            "rightSmall": None,
            "topSmall": None,
            "bottomSmall": None,
            "thumbnailSmall": "previews/file-123-small.webp",
            "thumbnailLarge": None,
        },
    ),
    (
        DfmAnalysisReadyEvent,
        "DfmAnalysisReadyEvent",
        "maliev.geometryservice.v1.dfm.ready",
        {
            "fileId": "file-123",
            "storagePath": "uploads/file-123.step",
            "analyzedAt": "2026-07-13T10:00:00Z",
        },
        "fileId",
        "file-123",
    ),
    (
        SmallThumbnailReadyEvent,
        "SmallThumbnailReadyEvent",
        "maliev.geometryservice.v1.thumbnail.small.ready",
        {
            "fileId": "file-123",
            "storagePath": "uploads/file-123.step",
            "thumbnailStoragePath": "previews/file-123-small.webp",
        },
        "thumbnailStoragePath",
        "previews/file-123-small.webp",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "event_name", "routing_key", "payload", "proof_key", "proof_value"),
    EVENT_CASES,
    ids=[case[1] for case in EVENT_CASES],
)
async def test_publish_event_uses_canonical_masstransit_envelope_contract(
    event_type: type[Any],
    event_name: str,
    routing_key: str,
    payload: dict[str, Any],
    proof_key: str,
    proof_value: Any,
) -> None:
    exchange = FakeExchange()
    initialize_event_publisher(exchange)  # type: ignore[arg-type]
    envelope_id = uuid4()
    correlation_id = uuid4()
    body_id = uuid4()
    occurred_at = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    message_type = (
        f"urn:message:Maliev.MessagingContracts.Contracts.Geometry:{event_name}"
    )
    event = event_type.model_validate(
        {
            "messageId": envelope_id,
            "correlationId": correlation_id,
            "messageType": [message_type],
            "headers": {"source": "GeometryService"},
            "message": {
                "messageId": body_id,
                "messageName": event_name,
                "messageType": "Event",
                "messageVersion": "1.0.0",
                "publishedBy": "GeometryService",
                "consumedBy": ["IntranetBff"],
                "correlationId": correlation_id,
                "causationId": None,
                "occurredAtUtc": occurred_at,
                "isPublic": False,
                "payload": payload,
            },
        }
    )

    await publish_event(event, routing_key)

    assert len(exchange.publications) == 1
    published, actual_routing_key = exchange.publications[0]
    assert actual_routing_key == routing_key
    assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert published.content_type == "application/vnd.masstransit+json"

    body = json.loads(published.body)
    assert set(body) == {
        "messageId",
        "correlationId",
        "conversationId",
        "sourceAddress",
        "destinationAddress",
        "messageType",
        "headers",
        "message",
    }
    assert UUID(body["messageId"]) == envelope_id
    assert UUID(body["correlationId"]) == correlation_id
    assert body["messageType"] == [message_type]
    assert body["headers"] == {"source": "GeometryService"}

    inner_message = body["message"]
    assert set(inner_message) == {
        "messageId",
        "messageName",
        "messageType",
        "messageVersion",
        "publishedBy",
        "consumedBy",
        "correlationId",
        "causationId",
        "occurredAtUtc",
        "isPublic",
        "payload",
    }
    assert UUID(inner_message["messageId"]) == body_id
    assert inner_message["messageName"] == event_name
    assert inner_message["messageType"] == "Event"
    assert inner_message["messageVersion"] == "1.0.0"
    assert inner_message["publishedBy"] == "GeometryService"
    assert inner_message["consumedBy"] == ["IntranetBff"]
    assert UUID(inner_message["correlationId"]) == correlation_id
    assert inner_message["occurredAtUtc"] == "2026-07-13T10:00:00Z"
    assert inner_message["isPublic"] is False
    assert inner_message["payload"][proof_key] == proof_value
