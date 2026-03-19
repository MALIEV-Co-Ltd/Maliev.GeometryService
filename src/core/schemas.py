from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.core.geometry import GeometryMetrics


def to_camel(string: str) -> str:
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


# ---------------------------------------------------------------------------
# MassTransit transport envelope (outer wrapper)
# Wire format: { "messageId": ..., "messageType": [...], "message": { ... } }
# ---------------------------------------------------------------------------


class MassTransitEnvelope(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    message_id: UUID = Field(alias="messageId")
    correlation_id: UUID | None = Field(alias="correlationId", default=None)
    conversation_id: UUID | None = Field(alias="conversationId", default=None)
    source_address: str | None = Field(alias="sourceAddress", default=None)
    destination_address: str | None = Field(alias="destinationAddress", default=None)
    message_type: list[str] = Field(alias="messageType", default_factory=list)
    headers: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared base for the inner "message" body (mirrors C# BaseMessage)
# All outgoing event bodies must include these fields so that C# consumers
# can deserialize them into the full event record (e.g. FileAnalyzedEvent).
# ---------------------------------------------------------------------------


class MessageTypeEnum(str, Enum):
    Event = "Event"
    Command = "Command"
    Request = "Request"
    Response = "Response"


class BaseMessageBody(BaseModel):
    """
    Mirrors the C# BaseMessage record.
    Every field published inside the MassTransit "message" envelope key must
    include these alongside the event-specific "payload" object.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    message_id: UUID = Field(alias="messageId")
    message_name: str = Field(alias="messageName")
    message_type: MessageTypeEnum = Field(alias="messageType")
    message_version: str = Field(alias="messageVersion", default="1.0.0")
    published_by: str = Field(alias="publishedBy")
    consumed_by: list[str] = Field(alias="consumedBy", default_factory=list)
    correlation_id: UUID | None = Field(alias="correlationId", default=None)
    causation_id: UUID | None = Field(alias="causationId", default=None)
    occurred_at_utc: datetime = Field(alias="occurredAtUtc")
    is_public: bool = Field(alias="isPublic", default=False)


# ---------------------------------------------------------------------------
# Incoming: FileUploadedEvent (published by C# UploadService — unchanged)
# ---------------------------------------------------------------------------


class UploadCompletedMessage(BaseModel):
    """The actual payload inside the FileUploadedEvent message."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    upload_id: str = Field(alias="uploadId")
    file_id: str | None = Field(alias="fileId", default=None)
    service_id: str = Field(alias="serviceId")
    file_name: str = Field(alias="fileName")
    storage_path: str = Field(alias="storagePath")
    download_url: str | None = Field(alias="downloadUrl", default=None)
    content_type: str = Field(alias="contentType")
    file_size: int = Field(alias="fileSize")
    uploaded_at: datetime = Field(alias="uploadedAt")


class FileUploadedMessage(BaseModel):
    """The message object published by UploadService."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    message_id: UUID = Field(alias="messageId")
    message_name: str = Field(alias="messageName")
    payload: UploadCompletedMessage


class FileUploadedEvent(MassTransitEnvelope):
    """Incoming event from UploadService wrapped in MassTransit envelope."""

    message: FileUploadedMessage


# ---------------------------------------------------------------------------
# Outgoing: FileAnalyzedEvent
# Wire format:
#   { "messageId": ..., "messageType": ["urn:...FileAnalyzedEvent"], "headers": {},
#     "message": {
#       "messageId": ..., "messageName": "FileAnalyzedEvent", "messageType": "Event",
#       "messageVersion": "1.0.0", "publishedBy": "GeometryService",
#       "consumedBy": [], "correlationId": ..., "causationId": null,
#       "occurredAtUtc": ..., "isPublic": false,
#       "payload": { "fileId": ..., "metrics": {...}, ... }
#     }
#   }
# ---------------------------------------------------------------------------


class FileAnalyzedPayload(BaseModel):
    """Nested payload data inside FileAnalyzedEvent.message — mirrors C# FileAnalyzedEventPayload."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: str = Field(alias="fileId")
    metrics: GeometryMetrics
    processed_at: datetime = Field(alias="processedAt")
    glb_storage_path: str | None = Field(alias="glbStoragePath", default=None)
    thumbnail_storage_path: str | None = Field(
        alias="thumbnailStoragePath", default=None
    )


class FileAnalyzedMessageBody(BaseMessageBody):
    """The full body of FileAnalyzedEvent.message — includes BaseMessage fields + payload."""

    payload: FileAnalyzedPayload


class FileAnalyzedEvent(MassTransitEnvelope):
    """Outgoing success event wrapped in MassTransit envelope."""

    message: FileAnalyzedMessageBody


# ---------------------------------------------------------------------------
# Outgoing: FileAnalysisFailedEvent
# ---------------------------------------------------------------------------


class FileAnalysisFailedPayload(BaseModel):
    """Nested payload data inside FileAnalysisFailedEvent.message."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: str = Field(alias="fileId")
    error_code: str = Field(alias="errorCode")
    details: str | None = Field(default=None)


class FileAnalysisFailedMessageBody(BaseMessageBody):
    """The full body of FileAnalysisFailedEvent.message."""

    payload: FileAnalysisFailedPayload


class FileAnalysisFailedEvent(MassTransitEnvelope):
    """Outgoing failure event wrapped in MassTransit envelope."""

    message: FileAnalysisFailedMessageBody


# ---------------------------------------------------------------------------
# Outgoing: PreviewImagesGeneratedEvent
# ---------------------------------------------------------------------------


class PreviewImagesMessage(BaseModel):
    """Seven preview image storage paths (6 orthographic + 1 isometric)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    front_256: str | None = Field(alias="front_256", default=None)
    back_256: str | None = Field(alias="back_256", default=None)
    left_256: str | None = Field(alias="left_256", default=None)
    right_256: str | None = Field(alias="right_256", default=None)
    top_256: str | None = Field(alias="top_256", default=None)
    bottom_256: str | None = Field(alias="bottom_256", default=None)
    iso_256: str | None = Field(alias="iso_256", default=None)


class PreviewImagesGeneratedPayload(BaseModel):
    """Nested payload data inside PreviewImagesGeneratedEvent.message."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    storage_path: str = Field(alias="storagePath")
    preview_images: PreviewImagesMessage = Field(alias="previewImages")
    generated_at: datetime = Field(alias="generatedAt")


class PreviewImagesGeneratedMessageBody(BaseMessageBody):
    """The full body of PreviewImagesGeneratedEvent.message."""

    payload: PreviewImagesGeneratedPayload


class PreviewImagesGeneratedEvent(MassTransitEnvelope):
    """Outgoing event for generated preview images."""

    message: PreviewImagesGeneratedMessageBody


# ---------------------------------------------------------------------------
# Keep old aliases so any other import sites don't break
# ---------------------------------------------------------------------------

# The inner payload types keep compatible names for upload_consumer.py imports
FileAnalyzedMessage = FileAnalyzedPayload
FileAnalysisFailedMessage = FileAnalysisFailedPayload
PreviewImagesGeneratedMessage = PreviewImagesGeneratedPayload
