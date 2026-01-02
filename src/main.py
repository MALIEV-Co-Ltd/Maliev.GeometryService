import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, responses
from fastapi.responses import JSONResponse
from scalar_fastapi import get_scalar_api_reference

from src.consumers.upload_consumer import UploadConsumer
from src.core.geometry import GeometryProcessor
from src.core.observability import setup_observability
from src.infrastructure.storage import HttpDownloadService

logger = logging.getLogger(__name__)

# Global consumer instance for cleanup
consumer: UploadConsumer | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    global consumer
    logger.info("Starting Geometry Service background consumer...")

    storage_service = HttpDownloadService()
    geometry_processor = GeometryProcessor()
    consumer = UploadConsumer(storage_service, geometry_processor)

    # Start consumer task in background
    consumer_task = asyncio.create_task(consumer.start())

    yield

    # Shutdown
    logger.info("Shutting down Geometry Service...")
    if consumer:
        # Note: We should implement a proper stop method in UploadConsumer
        # For now, cancelling the task is a start
        consumer_task.cancel()
        await storage_service.close()


app = FastAPI(
    title="Geometry Analysis Service",
    version="0.1.0",
    root_path="/geometry",
    docs_url="/openapi.json",
    lifespan=lifespan,
)

setup_observability(app)


@app.get("/scalar", include_in_schema=False)
async def scalar_html() -> responses.HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.root_path + "/openapi.json",
        title=app.title,
    )


@app.get("/liveness")
async def liveness() -> JSONResponse:
    """Kubernetes liveness probe."""
    return JSONResponse(content={"status": "alive"})


@app.get("/readiness")
async def readiness() -> JSONResponse:
    """Kubernetes readiness probe."""
    return JSONResponse(content={"status": "ready"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
