"""Tests for :mod:`omnigent.tools` package lazy exports."""

from __future__ import annotations

import pytest


def test_lazy_exports_resolve() -> None:
    import omnigent.tools as tools

    assert tools.ToolManager is not None
    assert tools.Tool is not None
    assert tools.ClientSideTool is not None


def test_unknown_attribute_raises() -> None:
    import omnigent.tools as tools

    with pytest.raises(AttributeError, match="has no attribute 'not_exported'"):
        _ = tools.not_exported