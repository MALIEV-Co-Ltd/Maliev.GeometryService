import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aio_pika
import aio_pika.abc

from src.core.config import settings

logger = logging.getLogger(__name__)

IAM_EXCHANGE = "maliev.iam"
IAM_ROUTING_KEY = "iam.permission.registration"


class IAMRegistrationClient:
    """Publishes IAM permission registration requests via RabbitMQ.

    This follows the same pattern as .NET BackgroundIAMRegistrationService,
    publishing a PermissionRegistrationRequest message that IAMService consumes.
    """

    def __init__(self) -> None:
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        """Connect to RabbitMQ and declare the IAM exchange."""
        max_retries = 10
        base_delay = 1.0
        max_delay = 30.0

        for attempt in range(max_retries):
            try:
                self._connection = await aio_pika.connect_robust(settings.RABBITMQ_URI)
                self._channel = await self._connection.channel()
                self._exchange = await self._channel.declare_exchange(
                    IAM_EXCHANGE, type=aio_pika.ExchangeType.TOPIC, durable=True
                )
                logger.info("Successfully connected to RabbitMQ for IAM registration")
                return
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to connect to RabbitMQ after {max_retries} attempts: {e}"
                    )
                    raise
                delay = min(base_delay * (2**attempt), max_delay)
                logger.warning(
                    f"RabbitMQ connection attempt {attempt + 1}/{max_retries} "
                    f"failed for IAM registration, retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)

    async def close(self) -> None:
        """Close the RabbitMQ connection."""
        if self._connection:
            await self._connection.close()
            logger.info("Closed RabbitMQ connection for IAM registration")

    async def register_permissions(self) -> bool:
        """Publish the permission registration request to RabbitMQ.

        Returns True if successful, False otherwise.
        """
        if not self._exchange:
            logger.error("Cannot register permissions: not connected to RabbitMQ")
            return False

        message = self._build_registration_message()

        try:
            await self._exchange.publish(
                aio_pika.Message(
                    body=message.encode("utf-8"),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=IAM_ROUTING_KEY,
            )
            logger.info(
                "Published IAM permission registration request for GeometryService: "
                "upload.files.upload"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to publish IAM registration request: {e}")
            return False

    def _build_registration_message(self) -> str:
        """Build the PermissionRegistrationRequest message JSON."""
        now = datetime.now(timezone.utc)
        message_id = str(uuid4())
        correlation_id = str(uuid4())

        message: dict[str, Any] = {
            "messageId": message_id,
            "messageName": "PermissionRegistrationRequest",
            "messageType": "Command",
            "messageVersion": "1.0.0",
            "publishedBy": "GeometryService",
            "consumedBy": ["iam-service"],
            "correlationId": correlation_id,
            "causationId": None,
            "occurredAtUtc": now.isoformat(),
            "isPublic": False,
            "serviceName": "GeometryService",
            "permissions": [
                {
                    "permissionId": "upload.files.upload",
                    "description": "Upload analysis artifacts to storage",
                }
            ],
            "roles": [],
        }

        return json.dumps(message)


async def register_iam_permissions() -> bool:
    """Convenience function to register IAM permissions.

    Connects to RabbitMQ, publishes the registration request, and closes the connection.

    Returns True if successful, False otherwise.
    """
    client = IAMRegistrationClient()
    try:
        await client.connect()
        success = await client.register_permissions()
        return success
    finally:
        await client.close()
