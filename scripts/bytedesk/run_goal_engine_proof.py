#!/usr/bin/env python3
"""Run the controlled Goal Engine flywheel proof and print JSON evidence."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from bytedesk_omnigent.engine.proof import run_controlled_flywheel_proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage",
        help="SQLAlchemy storage URL. Defaults to a temporary SQLite database.",
    )
    args = parser.parse_args()

    if args.storage:
        storage = args.storage
    else:
        tmp = tempfile.TemporaryDirectory(prefix="omnigent-goal-proof-")
        storage = f"sqlite:///{Path(tmp.name) / 'proof.db'}"

    print(json.dumps(run_controlled_flywheel_proof(storage), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
