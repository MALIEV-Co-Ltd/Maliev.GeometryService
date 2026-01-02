from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.core.geometry import GeometryMetrics


def to_camel(string: str) -> str:
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class MassTransitEnvelope(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    message_id: UUID = Field(alias="messageId")
    correlation_id: UUID | None = Field(alias="correlationId", default=None)
    conversation_id: UUID | None = Field(alias="conversationId", default=None)
    source_address: str | None = Field(alias="sourceAddress", default=None)
    destination_address: str | None = Field(alias="destinationAddress", default=None)
    message_type: list[str] = Field(alias="messageType", default_factory=list)
    headers: dict[str, Any] = Field(default_factory=dict)


class UploadCompletedMessage(BaseModel):
    """The actual payload inside the MassTransit envelope for upload.completed."""

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


class FileUploadedEvent(MassTransitEnvelope):
    """Incoming event from UploadService."""

    message: UploadCompletedMessage


class FileAnalyzedMessage(BaseModel):
    """Payload for FileAnalyzedEvent."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: str = Field(alias="fileId")
    metrics: GeometryMetrics
    processed_at: datetime = Field(alias="processedAt")


class FileAnalyzedEvent(MassTransitEnvelope):
    """Outgoing success event wrapped in MassTransit envelope."""

    message: FileAnalyzedMessage


class FileAnalysisFailedMessage(BaseModel):
    """Payload for FileAnalysisFailedEvent."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: str = Field(alias="fileId")
    error_code: str = Field(alias="errorCode")
    details: str | None = Field(default=None)


class FileAnalysisFailedEvent(MassTransitEnvelope):
    """Outgoing failure event wrapped in MassTransit envelope."""

    message: FileAnalysisFailedMessage
