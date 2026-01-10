import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import APIRouter, FastAPI, responses
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

    def on_task_done(t: asyncio.Task[None]) -> None:
        try:
            if not t.cancelled() and t.exception():
                logger.critical(f"Consumer task died with error: {t.exception()}")
        except asyncio.CancelledError:
            pass

    consumer_task.add_done_callback(on_task_done)

    yield

    # Shutdown
    logger.info("Shutting down Geometry Service...")
    if consumer:
        consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await consumer_task
        await storage_service.close()
        geometry_processor.shutdown()


app = FastAPI(
    title="MALIEV Geometry Analysis Service API",
    description=(
        "Dedicated 3D geometry analysis service for the Maliev platform. "
        "Provides automated processing of 3D mesh files (STL, OBJ, STEP) to "
        "extract critical manufacturing metrics including Volume, Surface Area, "
        "Axis-Aligned Bounding Box (AABB), and topological validity (Manifold "
        "status). Operating as an asynchronous worker, it integrates with "
        "the Upload Service and Quotation Service via RabbitMQ to provide "
        "real-time validation and cost estimation data."
    ),
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url="/geometry/openapi/v1.json",
    lifespan=lifespan,
)

setup_observability(app)

router = APIRouter(prefix="/geometry")


@router.get("/scalar", include_in_schema=False)
async def scalar_html() -> responses.HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )


@router.get("/aspire-liveness", include_in_schema=False)
async def aspire_liveness() -> JSONResponse:
    """Aspire-specific liveness check (minimal, fast)."""
    return JSONResponse(content={"status": "healthy"})


@router.get("/liveness", tags=["Health"])
async def liveness() -> JSONResponse:
    """Kubernetes liveness probe."""
    return JSONResponse(content={"status": "alive"})


@router.get("/readiness", tags=["Health"])
async def readiness() -> JSONResponse:
    """Kubernetes readiness probe."""
    return JSONResponse(content={"status": "ready"})


# Also add mirroring endpoints to the root app for extra robustness
@app.get("/liveness", include_in_schema=False)
@app.get("/readiness", include_in_schema=False)
@app.get("/aspire-liveness", include_in_schema=False)
async def root_health() -> JSONResponse:
    """Root level health checks."""
    return JSONResponse(content={"status": "healthy"})


app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
