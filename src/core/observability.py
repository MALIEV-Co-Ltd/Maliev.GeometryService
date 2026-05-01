import logging
import os

from fastapi import FastAPI
from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from src.core.config import settings

_is_configured = False


def setup_observability(app: FastAPI | None = None) -> None:
    """
    Configures OpenTelemetry for Tracing, Metrics, and Logging.
    Follows the pattern for .NET Aspire standalone Python integration.
    """
    global _is_configured

    # Set logging level for libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if not _is_configured:
        resource = Resource.create(attributes={SERVICE_NAME: settings.SERVICE_NAME})
        otlp_endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT

        # Skip OpenTelemetry setup if no endpoint configured (e.g., local development)
        if not otlp_endpoint:
            _is_configured = True
            return

        # --- 1. Setup Tracing ---
        tracer_provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(tracer_provider)

        # OTLP Trace Exporter
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

        # Optional: Console Span Exporter for local debugging
        if os.getenv("DEBUG_TELEMETRY", "false").lower() == "true":
            tracer_provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )

        # --- 2. Setup Metrics ---
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(meter_provider)

        # --- 3. Setup Logging ---
        logger_provider = LoggerProvider(resource=resource)
        _logs.set_logger_provider(logger_provider)

        # OTLP Log Exporter (sends to Aspire dashboard)
        log_exporter = OTLPLogExporter(endpoint=otlp_endpoint, insecure=True)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

        # Bridge Python logging to OpenTelemetry
        otel_handler = LoggingHandler(
            level=logging.NOTSET, logger_provider=logger_provider
        )

        # Configure root logger to include the OTel handler
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)  # root defaults to WARNING; must be INFO
        if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
            root_logger.addHandler(otel_handler)

        _is_configured = True

    if app:
        # Instrument FastAPI
        FastAPIInstrumentor.instrument_app(app)


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
