"""CORE first-party extension — a harness, plus a post_init composition step.

Shows two more lifecycle stages used through the SDK:
  * ``@harness`` contributes a harness factory into the ``harnesses`` seam.
  * a hand-written ``post_init`` (the SDK lets you override any lifecycle hook)
    runs AFTER every extension registered, so it can compose across the whole
    system — here it logs the fully-assembled capability manifest.
"""

from __future__ import annotations

import logging

from ..sdk import extension, harness
from .interfaces import ArtifactStore

logger = logging.getLogger("omnigent_demo.core.harnesses")


class ReplayHarness:
    name = "replay"

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def run(self) -> list[str]:
        return [f"{k} = {self._store.get(k)}" for k in self._store.keys()]


@extension(name="core.harnesses", requires=("core.stores",), entry_point=False)
class HarnessesExtension:
    @harness(name="replay")
    def replay_harness(self, store: ArtifactStore) -> ReplayHarness:
        return ReplayHarness(store)

    def post_init(self, host) -> None:
        # Stage 3: everything is registered; safe to introspect cross-extension.
        logger.info("core.harnesses sees seams: %s", host.manifest()["seams"])
