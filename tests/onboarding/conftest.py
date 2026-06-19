"""Shared fixtures for the onboarding unit suite.

These tests exercise the *local* keychain/OAuth-token flows, so they must be
hermetic against the developer's ambient Infisical credentials (BDP-2303): with
real creds in the shell, the Infisical extension backend would otherwise become
the primary secret store and try the network. Stub the extension seam to empty so
selection always resolves to the local keyring/file backend here.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding import secrets as _secrets


@pytest.fixture(autouse=True)
def _local_only_secret_store(monkeypatch):
    monkeypatch.setattr(_secrets, "_extension_backends", lambda: [])
    _secrets.reset_backends()
    yield
    _secrets.reset_backends()
