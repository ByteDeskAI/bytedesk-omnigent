"""``harness: grok-native`` wrap for the xAI Grok Build CLI (ACP over stdio)."""

from __future__ import annotations

import os

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.grok_native_executor import GrokNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_grok_native_executor() -> Executor:
    """
    Construct the grok ACP bridge executor.

    Model is read from ``HARNESS_GROK_MODEL`` (set by the spawn env from the
    agent spec's ``executor.model``); ``None`` lets grok pick its default.

    :returns: A :class:`GrokNativeExecutor`.
    """
    return GrokNativeExecutor(model=os.environ.get("HARNESS_GROK_MODEL") or None)


def create_app() -> FastAPI:
    """
    Build the ``grok-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_grok_native_executor)
    return adapter.build()
