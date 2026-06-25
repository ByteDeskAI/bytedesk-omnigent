"""Registry-shape tests for the LLM adapter factory (BDP-2471).

The provider dispatch is a single ``_ADAPTER_FACTORIES`` registry rather than a
hand-maintained if/elif plus a separately-listed ``all_providers`` set. These
tests pin the no-drift property the registry buys: the supported-provider list
is *derived* from the factory keys, and every registered key resolves.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters import (
    _ADAPTER_FACTORIES,
    clear_cache,
    get_adapter,
    supported_providers,
)
from omnigent.llms.adapters.base import BaseAdapter


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def test_supported_providers_is_the_registry_keys() -> None:
    assert supported_providers() == sorted(_ADAPTER_FACTORIES)


def test_unknown_provider_error_lists_the_registry() -> None:
    with pytest.raises(OmnigentError) as exc:
        get_adapter("not-a-provider")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    # The error's supported list must be exactly the registry's keys — the
    # drift that the old hand-maintained ``all_providers`` set risked.
    assert str(supported_providers()) in str(exc.value)


@pytest.mark.parametrize("provider", sorted(_ADAPTER_FACTORIES))
def test_every_registered_provider_resolves(provider: str) -> None:
    adapter = get_adapter(provider)
    assert isinstance(adapter, BaseAdapter)
