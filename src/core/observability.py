import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from src.core.config import settings


def setup_observability(app: FastAPI | None = None) -> None:
    # Setup Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Setup Tracing
    resource = Resource.create(attributes={SERVICE_NAME: settings.SERVICE_NAME})

    provider = TracerProvider(resource=resource)
    # Default to console exporter if no OTLP endpoint is configured or for dev
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    if app:
        FastAPIInstrumentor.instrument_app(app)


tracer = trace.get_tracer(__name__)
