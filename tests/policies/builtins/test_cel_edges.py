"""Edge-case coverage for :mod:`omnigent.policies.builtins.cel`."""

from __future__ import annotations

import pytest

import omnigent.policies.builtins.cel as cel_mod


def test_cel_policy_raises_when_runtime_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cel_mod, "_cel", None)
    with pytest.raises(ImportError, match=r"cel-expr-python"):
        cel_mod.cel_policy(expression='{"result": "ALLOW"}')