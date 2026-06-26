from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _imports_nats(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "nats" or alias.name.startswith("nats.") for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "nats" or node.module.startswith("nats."):
                return True
    return False


def test_new_fabric_routes_and_policies_do_not_import_raw_nats() -> None:
    checked_roots = [
        _ROOT / "bytedesk_omnigent" / "routes",
        _ROOT / "bytedesk_omnigent" / "scheduler",
        _ROOT / "bytedesk_omnigent" / "fabric",
    ]
    allowed = {
        _ROOT / "omnigent" / "fabric" / "nats_adapter.py",
    }
    offenders: list[str] = []
    for root in checked_roots:
        for path in root.rglob("*.py"):
            if path in allowed:
                continue
            if _imports_nats(path):
                offenders.append(str(path.relative_to(_ROOT)))

    assert offenders == []


def test_session_routes_do_not_call_legacy_host_launch_directly() -> None:
    source = (_ROOT / "omnigent" / "server" / "routes" / "sessions.py").read_text()

    assert "from omnigent.server.host_control import" not in source
    assert "request_host_launch_runner(" not in source
    assert "HostWorkerRunnerFabric" in source


def test_host_launch_route_uses_fabric_facade() -> None:
    source = (_ROOT / "omnigent" / "server" / "routes" / "hosts.py").read_text()

    assert "request_host_launch_runner(" not in source
    assert "HostWorkerRunnerFabric" in source


def test_runner_transport_factory_registers_nats_only() -> None:
    source = (_ROOT / "omnigent" / "runner" / "transports" / "factory.py").read_text()

    assert "NatsRunnerTransport" in source
    assert "omnigent.runner.transports.tcp" not in source
    assert "omnigent.runner.transports.uds" not in source
    assert "websockets" not in source
    assert "OMNIGENT_RUNNER_TRANSPORT" not in source


def test_legacy_server_uds_runner_transport_module_removed() -> None:
    assert not (_ROOT / "omnigent" / "server" / "_runner_transport.py").exists()


def test_lifespan_clears_legacy_runner_ws_factory() -> None:
    source = (_ROOT / "omnigent" / "kernel" / "lifespan_phases.py").read_text()

    assert "class RunnerWsFactoryPhase" in source
    assert "Ensure no legacy runner WS factory is installed" in source
    assert "set_runner_ws_factory(None)" in source


def test_monolithic_lifespan_does_not_register_runner_ws_factory() -> None:
    source = (_ROOT / "omnigent" / "server" / "app.py").read_text()

    assert "set_runner_ws_factory(None)" in source
    assert "set_runner_ws_factory(ws_factory)" not in source
    assert "build_uds_runner(" not in source
