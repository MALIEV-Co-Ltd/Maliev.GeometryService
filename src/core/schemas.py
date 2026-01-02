from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.core.geometry import GeometryMetrics


def to_camel(string: str) -> str:
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class BaseMessage(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    message_id: UUID = Field(alias="messageId")
    correlation_id: UUID = Field(alias="correlationId")


class FileUploadedPayload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: UUID = Field(alias="fileId")
    storage_bucket: str = Field(alias="storageBucket")
    storage_key: str = Field(alias="storageKey")
    download_url: str = Field(alias="downloadUrl")
    content_type: str = Field(alias="contentType")
    uploaded_at: datetime = Field(alias="uploadedAt")


class FileUploadedEvent(BaseMessage):
    payload: FileUploadedPayload


class FileAnalyzedPayload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: UUID = Field(alias="fileId")
    metrics: GeometryMetrics
    processed_at: datetime = Field(alias="processedAt")


class FileAnalyzedEvent(BaseMessage):
    payload: FileAnalyzedPayload


class FileAnalysisFailedPayload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    file_id: UUID = Field(alias="fileId")
    error_code: str = Field(alias="errorCode")
    details: str | None = None


class FileAnalysisFailedEvent(BaseMessage):
    payload: FileAnalysisFailedPayload
