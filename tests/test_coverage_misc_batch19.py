"""Batch-19 coverage for small omnigent/bytedesk_omnigent module gaps."""

from __future__ import annotations

import runpy

import pytest

from omnigent import _build_info
from omnigent.coordination.sync import claim_resource, release_resource
from omnigent.errors import ErrorCode, StaleWriteError
from omnigent.session_lifecycle import (
    CLOSED_TITLE_INFIX,
    has_closed_title_marker,
    is_session_closed,
    title_without_closed_marker,
)


def test_stale_write_error_sets_precondition_failed_code() -> None:
    err = StaleWriteError("concurrent edit")
    assert err.code == ErrorCode.PRECONDITION_FAILED
    assert str(err) == "concurrent edit"


def test_title_without_closed_marker_none_returns_none() -> None:
    assert title_without_closed_marker(None) is None


def test_title_without_closed_marker_strips_legacy_suffix() -> None:
    title = f"researcher:auth{CLOSED_TITLE_INFIX}conv_abc"
    assert title_without_closed_marker(title) == "researcher:auth"
    assert title_without_closed_marker("plain-title") == "plain-title"


def test_has_closed_title_marker_and_is_session_closed() -> None:
    closed = f"agent:task{CLOSED_TITLE_INFIX}conv_x"
    assert has_closed_title_marker(closed) is True
    assert has_closed_title_marker("open") is False
    assert has_closed_title_marker(None) is False
    assert is_session_closed(None, closed) is True
    assert is_session_closed({"omnigent.closed": "true"}) is True


def test_coordination_sync_noops_when_backplane_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.coordination.lifecycle.get_active_backplane",
        lambda: None,
    )
    claim_resource("runner", "runner_1")
    release_resource("runner", "runner_1")


def test_omnigent_tools_lazy_export_and_unknown_attr() -> None:
    import omnigent.tools as tools

    assert tools.Tool is not None
    with pytest.raises(AttributeError, match="has no attribute"):
        _ = tools.NotARealExportName


def test_bytedesk_realtime_package_reexports() -> None:
    import bytedesk_omnigent.realtime as realtime

    assert callable(realtime.install_realtime_bridge)
    assert callable(realtime.emit_roster)
    assert callable(realtime.emit_presence)


def test_bytedesk_secrets_package_reexports() -> None:
    from bytedesk_omnigent.secrets import InfisicalBackend

    assert InfisicalBackend is not None


def test_build_info_constants_are_importable() -> None:
    assert isinstance(_build_info.BUILD_TIME_EPOCH, int)
    assert isinstance(_build_info.COMMIT_SHA, str)
    assert _build_info.COMMIT_SHA


def test_omnigent_main_module_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def _fake_main() -> None:
        called.append(True)

    monkeypatch.setattr("omnigent.cli.main", _fake_main)
    runpy.run_module("omnigent.__main__", run_name="__main__")
    assert called == [True]


def test_runtime_executors_package_reexports() -> None:
    from omnigent.runtime.executors import (
        Executor,
        TurnComplete,
        dict_to_event,
        event_to_dict,
    )

    assert Executor is not None
    assert TurnComplete is not None
    assert callable(dict_to_event)
    assert callable(event_to_dict)


def test_sandbox_seatbelt_reexport() -> None:
    from omnigent.sandbox.seatbelt import SeatbeltSandboxBackend

    assert SeatbeltSandboxBackend is not None