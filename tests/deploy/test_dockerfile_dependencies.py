from __future__ import annotations

from pathlib import Path


def _lines(path: str) -> list[str]:
    return Path(path).read_text().splitlines()


def _nats_install_indexes(lines: list[str]) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if "uv pip install" in line and "nats-py" in line
    ]


def test_standard_host_image_inherits_nats_transport_dependency() -> None:
    lines = _lines("deploy/docker/Dockerfile")

    nats_installs = _nats_install_indexes(lines)
    server_builder = next(
        index for index, line in enumerate(lines) if line == "FROM builder AS server-builder"
    )

    assert len(nats_installs) == 1
    assert nats_installs[0] < server_builder


def test_ubi_host_image_inherits_nats_transport_dependency() -> None:
    lines = _lines("deploy/docker/Dockerfile.ubi")

    nats_installs = _nats_install_indexes(lines)
    server_builder = next(
        index for index, line in enumerate(lines) if line == "FROM builder AS server-builder"
    )

    assert len(nats_installs) == 1
    assert nats_installs[0] < server_builder
