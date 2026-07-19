from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from src.main import lifespan


@pytest.mark.asyncio
async def test_lifespan():
    app_mock = MagicMock(spec=FastAPI)

    # Mock dependencies that lifespan uses
    with (
        patch("src.main.register_iam_permissions", new_callable=AsyncMock) as mock_iam,
        patch("src.main.HttpDownloadService") as mock_storage,
        patch("src.main.GeometryProcessor") as mock_geometry,
        patch("src.main.UploadConsumer") as mock_consumer_class,
        patch("asyncio.create_task") as mock_create_task,
    ):
        # register_iam_permissions must not make real network calls
        mock_iam.return_value = True

        # Setup mock instances
        mock_storage_instance = mock_storage.return_value
        mock_storage_instance.close = AsyncMock()

        mock_geometry_instance = mock_geometry.return_value
        mock_geometry_instance.shutdown = MagicMock()

        mock_consumer_instance = mock_consumer_class.return_value
        mock_consumer_instance.start = AsyncMock()
        mock_consumer_instance.stop = AsyncMock()

        # Create a real task-like mock that is awaitable
        import asyncio

        mock_task = asyncio.Future()

        def fake_create_task(coro):
            coro.close()
            return mock_task

        mock_create_task.side_effect = fake_create_task

        async with lifespan(app_mock):
            # Startup happened
            mock_iam.assert_called_once()
            mock_storage.assert_called_once()
            mock_geometry.assert_called_once()
            mock_consumer_class.assert_called_once()
            mock_create_task.assert_called_once()

        # Shutdown happened
        assert mock_task.cancelled()
        mock_consumer_instance.stop.assert_called_once()
        mock_storage_instance.close.assert_called_once()
        mock_geometry_instance.shutdown.assert_called_once()
