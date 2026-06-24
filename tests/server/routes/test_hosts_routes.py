"""Tests for the hosts REST routes (``/v1/hosts``).

The hosts router is only mounted when ``host_store`` is provided to
``create_app``. The standard test ``app`` fixture does not supply one,
so host endpoints return 404. These tests verify the expected behavior
when hosts are not configured, and test the route helpers directly.
"""

from __future__ import annotations

import httpx


async def test_hosts_not_mounted_without_host_store(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts is not the hosts JSON API when host_store is unset."""
    resp = await client.get("/v1/hosts")
    # Without host_store the hosts router is not mounted; the SPA catch-all
    # serves index.html instead of {"hosts": [...]}.
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_get_host_not_mounted(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts/{id} is not the hosts JSON API when host_store is unset."""
    resp = await client.get("/v1/hosts/host_nonexistent_12345")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
