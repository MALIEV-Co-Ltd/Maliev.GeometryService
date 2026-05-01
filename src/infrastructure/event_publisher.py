"""Standalone event publisher for GeometryService events.

This module provides a reusable publish_event function that can be imported
by both the upload consumer and the FastAPI endpoints (main.py).
"""

import logging

import aio_pika
import aio_pika.abc

from src.core.schemas import (
    DfmAnalysisReadyEvent,
    FileAnalysisFailedEvent,
    FileAnalyzedEvent,
    FileMetricsReadyEvent,
    PreviewImagesGeneratedEvent,
    SmallThumbnailReadyEvent,
)

logger = logging.getLogger(__name__)

# Global exchange reference - must be initialized at startup
_exchange: aio_pika.abc.AbstractRobustExchange | None = None


def initialize_event_publisher(exchange: aio_pika.abc.AbstractRobustExchange) -> None:
    """Initialize the global event publisher with an aio_pika exchange.

    Args:
        exchange: The aio_pika exchange to use for publishing events.
                  Should be the 'maliev.events' topic exchange.
    """
    global _exchange
    _exchange = exchange
    logger.info("Event publisher initialized with exchange")


async def publish_event(
    event: FileAnalyzedEvent
    | FileAnalysisFailedEvent
    | FileMetricsReadyEvent
    | PreviewImagesGeneratedEvent
    | DfmAnalysisReadyEvent
    | SmallThumbnailReadyEvent,
    routing_key: str,
) -> None:
    """Publish an event to the message bus.

    Args:
        event: The event object to publish (must be a Pydantic model).
        routing_key: The routing key for the message (e.g., 'maliev.geometryservice.v1.dfm.ready').

    Raises:
        RuntimeError: If the exchange has not been initialized via initialize_event_publisher().
    """  # noqa: E501
    if _exchange is None:
        raise RuntimeError(
            "Event publisher not initialized. Call initialize_event_publisher(exchange) first."  # noqa: E501
        )

    # model_dump_json(by_alias=True) ensures camelCase for MassTransit
    message_body = event.model_dump_json(by_alias=True).encode()
    await _exchange.publish(
        aio_pika.Message(
            body=message_body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=routing_key,
    )
    logger.debug(f"Published event to {routing_key}: {type(event).__name__}")
