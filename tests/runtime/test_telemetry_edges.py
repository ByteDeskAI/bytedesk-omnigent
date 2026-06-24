"""Edge-case coverage for omnigent.runtime.telemetry."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from omnigent.runtime import telemetry

_RESP_HEX = "d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"
_RESP_ID = f"resp_{_RESP_HEX}"


@pytest.fixture(autouse=True)
def _reset_telemetry_init_guards(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    yield


def test_instrument_fastapi_app_logs_instrumentation_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FastAPI instrumentation failures are logged without raising."""
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")

    def _boom(_app: FastAPI) -> None:
        raise RuntimeError("instrumentation unavailable")

    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        _boom,
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runtime.telemetry"):
        telemetry.instrument_fastapi_app(FastAPI())

    assert any(
        "failed to initialize FastAPI OpenTelemetry instrumentation" in record.message
        for record in caplog.records
    )


def test_patch_mlflow_otel_remote_parent_spans_skips_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing MLflow OTLP processor modules skip the patch quietly."""
    real_import = __import__

    def _import_without_mlflow_span(
        name: str,
        globals: object | None = None,
        locals: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "mlflow.entities.span":
            raise ImportError("mlflow span helpers unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _import_without_mlflow_span)

    with caplog.at_level(logging.DEBUG, logger="omnigent.runtime.telemetry"):
        telemetry._patch_mlflow_otel_remote_parent_spans()

    assert any(
        "MLflow OTLP span processor patch skipped" in record.message
        for record in caplog.records
    )


def test_patch_mlflow_on_start_delegates_when_trace_registration_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """patched_on_start falls back to BatchSpanProcessor when registration is off."""
    from mlflow.tracing.processor.otel import OtelSpanProcessor
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    telemetry._patch_mlflow_otel_remote_parent_spans()

    processor = MagicMock(spec=OtelSpanProcessor)
    processor._should_register_traces = False
    processor._trace_manager = object()

    span = MagicMock()
    span.parent = None
    span.context.trace_id = int(_RESP_HEX, 16)

    called: list[tuple[Any, Any, Any]] = []

    def _record_on_start(
        self: Any,
        span_arg: Any,
        *,
        parent_context: Any = None,
    ) -> None:
        called.append((self, span_arg, parent_context))

    monkeypatch.setattr(BatchSpanProcessor, "on_start", _record_on_start)
    OtelSpanProcessor.on_start(processor, span, parent_context=None)

    assert called == [(processor, span, None)]
    processor._create_trace_info.assert_not_called()


def test_patch_mlflow_on_start_delegates_when_trace_manager_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """patched_on_start falls back when MLflow's trace manager is absent."""
    from mlflow.tracing.processor.otel import OtelSpanProcessor
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    telemetry._patch_mlflow_otel_remote_parent_spans()

    processor = MagicMock(spec=OtelSpanProcessor)
    processor._should_register_traces = True
    processor._trace_manager = None

    span = MagicMock()
    span.parent = None
    span.context.trace_id = int(_RESP_HEX, 16)

    called: list[tuple[Any, Any, Any]] = []

    def _record_on_start(
        self: Any,
        span_arg: Any,
        *,
        parent_context: Any = None,
    ) -> None:
        called.append((self, span_arg, parent_context))

    monkeypatch.setattr(BatchSpanProcessor, "on_start", _record_on_start)
    OtelSpanProcessor.on_start(processor, span, parent_context=None)

    assert called == [(processor, span, None)]


def test_get_traceparent_env_includes_tracestate_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess env propagation includes TRACESTATE when the carrier has one."""

    class _Propagator:
        def inject(self, carrier: dict[str, str]) -> None:
            carrier["traceparent"] = (
                f"00-{_RESP_HEX}-1000000000000001-01"
            )
            carrier["tracestate"] = "vendor=testvalue"

    monkeypatch.setattr(
        "opentelemetry.trace.propagation.tracecontext.TraceContextTextMapPropagator",
        _Propagator,
    )

    env = telemetry.get_traceparent_env()
    assert env == {
        "TRACEPARENT": f"00-{_RESP_HEX}-1000000000000001-01",
        "TRACESTATE": "vendor=testvalue",
    }


@pytest.mark.parametrize(
    "endpoint,configured,expected",
    [
        ("http://collector:4317", None, "otlp"),
        ("http://collector:4317", "PROMETHEUS", "prometheus"),
        (None, "none", "none"),
        (None, None, "none"),
    ],
)
def test_metrics_exporter_name_resolution(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str | None,
    configured: str | None,
    expected: str,
) -> None:
    """OTEL metrics exporter name follows env precedence."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    if endpoint is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    if configured is not None:
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", configured)
    assert telemetry._metrics_exporter_name() == expected


@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("grpc", "grpc"),
        ("", "grpc"),
        ("http/protobuf", "http/protobuf"),
    ],
)
def test_otlp_protocol_supported_values(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    expected: str,
) -> None:
    """Supported OTLP protocol env values normalize to grpc or http/protobuf."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    assert telemetry._otlp_protocol() == expected


def test_otlp_protocol_rejects_unknown_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported OTLP protocol values raise ValueError."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "kafka")
    with pytest.raises(ValueError, match="Unsupported OTLP protocol"):
        telemetry._otlp_protocol()


def test_create_otlp_metric_exporter_grpc_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default OTLP metrics exporter uses the gRPC backend."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter as GrpcExporter,
    )

    exporter = telemetry._create_otlp_metric_exporter()
    assert isinstance(exporter, GrpcExporter)


def test_create_otlp_metric_exporter_http_protobuf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP/protobuf OTLP protocol selects the HTTP metric exporter."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as HttpExporter,
    )

    exporter = telemetry._create_otlp_metric_exporter()
    assert isinstance(exporter, HttpExporter)


def test_build_metric_exporter_registry_registers_otlp_default() -> None:
    """Metric exporter registry exposes the built-in OTLP backend."""
    registry = telemetry._build_metric_exporter_registry()
    assert registry.names() == ["otlp"]


def test_create_metric_exporter_resolves_registered_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered exporter names construct via the pluggable registry."""
    sentinel = object()
    monkeypatch.setattr(telemetry, "_create_otlp_metric_exporter", lambda: sentinel)
    assert telemetry._create_metric_exporter("otlp") is sentinel


def test_init_otel_metrics_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated metrics init is a no-op after the first successful pass."""
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    telemetry._init_otel_metrics()
    assert telemetry._metrics_initialized is True
    telemetry._init_otel_metrics()
    assert telemetry._metrics_initialized is True


def test_init_otel_metrics_warns_on_unsupported_exporter(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown OTEL_METRICS_EXPORTER values disable export with a warning."""
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "prometheus")
    with caplog.at_level(logging.WARNING, logger="omnigent.runtime.telemetry"):
        telemetry._init_otel_metrics()
    assert telemetry._metrics_initialized is True
    assert any(
        "unsupported OTEL_METRICS_EXPORTER=prometheus" in record.message
        for record in caplog.records
    )


def test_init_otel_metrics_configures_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTLP metrics export wires a MeterProvider when configured."""
    from opentelemetry import metrics as otel_metrics

    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "omnigent-test")

    fake_exporter = object()
    monkeypatch.setattr(telemetry, "_create_otlp_metric_exporter", lambda: fake_exporter)

    created_readers: list[Any] = []

    class _RecordingReader:
        def __init__(self, exporter: object) -> None:
            created_readers.append(exporter)

    monkeypatch.setattr(
        "opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader",
        _RecordingReader,
    )

    providers: list[Any] = []

    class _RecordingProvider:
        def __init__(self, *, metric_readers: list[Any], resource: Any) -> None:
            self.metric_readers = metric_readers
            self.resource = resource
            providers.append(self)

    monkeypatch.setattr("opentelemetry.sdk.metrics.MeterProvider", _RecordingProvider)
    set_calls: list[Any] = []
    monkeypatch.setattr(otel_metrics, "set_meter_provider", set_calls.append)

    telemetry._init_otel_metrics()

    assert telemetry._metrics_initialized is True
    assert created_readers == [fake_exporter]
    assert len(providers) == 1
    assert set_calls == [providers[0]]


def test_init_otel_metrics_logs_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Metrics init failures are logged and leave metrics disabled."""
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")

    def _raise() -> object:
        raise RuntimeError("metrics backend unavailable")

    monkeypatch.setattr(telemetry, "_create_otlp_metric_exporter", _raise)

    with caplog.at_level(logging.ERROR, logger="omnigent.runtime.telemetry"):
        telemetry._init_otel_metrics()

    assert telemetry._metrics_initialized is True
    assert any(
        "failed to initialize OpenTelemetry metrics" in record.message
        for record in caplog.records
    )


def test_init_handles_missing_mlflow_tracing_package(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """init() degrades quietly when mlflow.tracing is not importable."""
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")

    real_import = __import__

    def _import_without_mlflow_tracing(
        name: str,
        globals: object | None = None,
        locals: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "mlflow.tracing" or (
            name == "mlflow" and fromlist and "tracing" in fromlist
        ):
            raise ImportError("mlflow tracing extra not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _import_without_mlflow_tracing)

    with caplog.at_level(logging.INFO, logger="omnigent.runtime.telemetry"):
        telemetry.init()

    assert telemetry._initialized is True
    assert any("MLflow not installed" in record.message for record in caplog.records)


def test_init_logs_tracing_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected MLflow tracing failures are logged without aborting startup."""
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")

    import mlflow.tracing

    def _boom() -> None:
        raise RuntimeError("tracing enable failed")

    monkeypatch.setattr(mlflow.tracing, "enable", _boom)

    with caplog.at_level(logging.ERROR, logger="omnigent.runtime.telemetry"):
        telemetry.init()

    assert telemetry._initialized is True
    assert any(
        "failed to initialize MLflow tracing" in record.message
        for record in caplog.records
    )


