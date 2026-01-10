import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.core.config import settings

logger = logging.getLogger(__name__)


def setup_observability(app: FastAPI | None = None) -> None:
    # Set logging level for libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Setup Tracing
    resource = Resource.create(attributes={SERVICE_NAME: settings.SERVICE_NAME})

    provider = TracerProvider(resource=resource)

    # Use OTLP Exporter if endpoint is configured, otherwise (or as fallback)
    # could use Console. But for Aspire, we want OTLP.
    # The verbose JSON was from ConsoleSpanExporter.
    logger.info("Setting up OTLP Exporter to %s", settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
    )

    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    if app:
        FastAPIInstrumentor.instrument_app(app)


tracer = trace.get_tracer(__name__)
