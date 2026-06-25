#!/usr/bin/env python3
"""End-to-end boot of the three-tier microkernel prototype.

Run from the prototype/ directory:

    python3 run_demo.py
    OMNIGENT_USE_ARTIFACT_STORE=s3 python3 run_demo.py   # swap an impl, zero consumer changes

What it demonstrates, in order:
  1. KERNEL boots a HOST; CORE first-party extensions register through the SDK.
  2. A third-party EXTENSION (bytedesk) self-registers via entry-point discovery.
  3. Lifecycle stages fire in order (pre_init → register → post_init → after_init).
  4. DI resolves tools by INTERFACE — the impl is swappable (strangler flag).
  5. A third-party extension REPLACES the Clock interface — any part is pluggable.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="  · %(name)s: %(message)s")

# Importing the extensions package makes the third-party extension self-register
# (the demo stand-in for `pip install`-ed entry-point metadata).
import omnigent_demo.extensions  # noqa: E402,F401
from omnigent_demo import bootstrap  # noqa: E402
from omnigent_demo.core.interfaces import ArtifactStore, Clock  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * (60 - len(title))}")


def main() -> None:
    store_impl = os.environ.get("OMNIGENT_USE_ARTIFACT_STORE", "memory")

    rule("1. BOOT: kernel + core + discovered third-party extensions")
    host = bootstrap(discover=True)
    print(f"  loaded plugins (load order): {[p.name for p in host.plugins]}")

    rule("2. CAPABILITY MANIFEST (everything that self-wired)")
    manifest = host.manifest()
    for seam, names in manifest["seams"].items():
        if names:
            print(f"  seam {seam:<16} -> {names}")
    print(f"  DI services         -> {manifest['services']}")

    rule(f"3. DI BY INTERFACE (active ArtifactStore impl = {store_impl!r})")
    store = host.resolve(ArtifactStore)
    clock = host.resolve(Clock)
    print(f"  resolved ArtifactStore -> {type(store).__name__}")
    print(f"  resolved Clock         -> {type(clock).__name__}  (replaced by bytedesk ext)")

    rule("4. USE A TOOL (its store + clock were injected by interface)")
    record = host.seams["tools"].get("record")  # core.tools tool
    print("  " + record("greeting", "hello world"))
    print("  " + record("farewell", "goodbye"))

    rule("5. CROSS-EXTENSION: third-party 'audit' tool reads core's store")
    audit = host.seams["tools"].get("audit")  # bytedesk ext tool
    print(f"  audit() sees keys: {audit()}")

    rule("6. A CORE HARNESS replays from the same injected store")
    replay = host.seams["harnesses"].get("replay")
    for line in replay.run():
        print(f"  {line}")

    rule("7. SCOPED DI (per-request isolation)")
    scope = host.container.create_scope()
    print(f"  scope shares singleton store: {scope.resolve(ArtifactStore) is store}")

    print("\n✓ kernel → core → extensions booted; DI + interface-swapping verified.\n")


if __name__ == "__main__":
    main()
