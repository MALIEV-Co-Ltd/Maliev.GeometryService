import logging
import unittest.mock as mock

from src.core import observability as obs_module


def _reset_observability():
    obs_module._is_configured = False
    from opentelemetry.sdk._logs import LoggingHandler
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, LoggingHandler)]


def test_root_logger_level_set_to_info_when_otlp_configured(monkeypatch):
    """Root logger must be at INFO when OTLP endpoint is configured.

    Before the fix, root logger stayed at WARNING (default). Every INFO log
    from child loggers was silently dropped before reaching the OTel handler.
    """
    _reset_observability()

    monkeypatch.setattr(obs_module.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(obs_module.settings, "SERVICE_NAME", "test-service")

    with mock.patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
         mock.patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"), \
         mock.patch("opentelemetry.exporter.otlp.proto.grpc._log_exporter.OTLPLogExporter"):
        obs_module.setup_observability()

    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO, (
        f"Root logger level should be INFO ({logging.INFO}) "
        f"but got {root_logger.level} ({logging.getLevelName(root_logger.level)})"
    )
    _reset_observability()
