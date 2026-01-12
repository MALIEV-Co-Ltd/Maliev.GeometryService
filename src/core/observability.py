import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from src.core.config import settings


class JsonFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging compatible with Maliev.Aspire.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "service.name": settings.SERVICE_NAME,
        }

        # Inject Trace Context if available
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            ctx = span.get_span_context()
            log_entry["trace_id"] = trace.format_trace_id(ctx.trace_id)
            log_entry["span_id"] = trace.format_span_id(ctx.span_id)

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


def setup_observability(app: FastAPI | None = None) -> None:
    # Configure root logger with JSON formatter
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clean up existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(console_handler)

    # Setup Tracing
    resource = Resource.create(attributes={SERVICE_NAME: settings.SERVICE_NAME})

    provider = TracerProvider(resource=resource)
    # Default to console exporter for now
    # In production, we would use OTLPSpanExporter
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    if app:
        FastAPIInstrumentor.instrument_app(app)


tracer = trace.get_tracer(__name__)
