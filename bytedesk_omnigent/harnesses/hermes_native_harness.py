"""``harness: hermes`` wrap for the local Hermes Agent (ACP over stdio)."""

from __future__ import annotations

import os

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

from bytedesk_omnigent.harnesses.hermes_native_executor import HermesNativeExecutor


def _build_hermes_native_executor() -> Executor:
    """
    Construct the Hermes ACP bridge executor.

    Model is read from ``HARNESS_HERMES_MODEL`` (set by the spawn env from the
    agent spec's ``executor.model``); ``None`` lets Hermes pick its own model
    (model-agnostic — do not default a model).

    :returns: A :class:`HermesNativeExecutor`.
    """
    return HermesNativeExecutor(model=os.environ.get("HARNESS_HERMES_MODEL") or None)


def create_app() -> FastAPI:
    """
    Build the ``hermes`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_hermes_native_executor)
    return adapter.build()
