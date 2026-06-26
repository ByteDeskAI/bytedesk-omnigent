"""Shared fabric worker-loop template."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class FabricWorkerLoop(ABC):
    """Template Method for consume, ack/nak/term, drain, and shutdown."""

    def __init__(self) -> None:
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.connect()
        try:
            async for message in self.consume():
                if self._stopping.is_set():
                    await self.nak(message)
                    break
                try:
                    async with self.in_progress(message):
                        await self.handle(message)
                except asyncio.CancelledError:
                    await self.nak(message)
                    raise
                except Exception:  # noqa: BLE001 - worker loop owns DLQ/nak handling
                    logger.warning("fabric worker message failed", exc_info=True)
                    await self.nak(message)
                else:
                    await self.ack(message)
        finally:
            await self.drain()
            await self.shutdown()

    def stop(self) -> None:
        self._stopping.set()

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the backing fabric."""

    @abstractmethod
    async def consume(self) -> Any:
        """Yield messages from the backing fabric."""

    @abstractmethod
    async def handle(self, message: Any) -> None:
        """Process a single message."""

    @abstractmethod
    async def ack(self, message: Any) -> None:
        """Ack a processed message."""

    @abstractmethod
    async def nak(self, message: Any) -> None:
        """Nak a failed or interrupted message."""

    async def term(self, message: Any, reason: str) -> None:
        del message, reason

    @contextlib.asynccontextmanager
    async def in_progress(self, message: Any) -> Any:
        del message
        yield

    async def drain(self) -> None:
        """Drain in-flight work before shutdown."""
        if self._stopping.is_set():
            return

    async def shutdown(self) -> None:
        """Release transport resources."""
        if self._stopping.is_set():
            return
