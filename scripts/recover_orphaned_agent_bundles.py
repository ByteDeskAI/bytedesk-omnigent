#!/usr/bin/env python3
"""Detect (and optionally re-hydrate) agent bundles orphaned from the artifact store.

BDP-2382 — companion to the NATS Object Store durability fix (BDP-2380/2381,
ADR-0148). The durable store prevents *future* bundle loss on pod rolls; it
cannot resurrect bytes already lost with the old per-pod ``emptyDir``. This
tool finds agents whose DB ``bundle_location`` has no object in the store and
classifies each so recovery is targeted, not guesswork.

Run inside an omnigent-server pod (uses its venv + in-cluster NATS):

    python scripts/recover_orphaned_agent_bundles.py            # report only
    python scripts/recover_orphaned_agent_bundles.py --rehydrate ag_<id> <bundle.tar.gz>

Classification:
  * ``broken``   — image endpoint 5xx: bundle truly missing, agent unusable.
                   Recovery = re-upload the bundle (PUT /v1/agents/{id}/image)
                   from its source. For migrated (omnigent-SoT) agents the
                   source is the platform agent-config sync (ADR-0115) or the
                   original bundle; bytes lost from emptyDir are NOT here.
  * ``stale``    — image endpoint 4xx / duplicate / test/demo row: a stale
                   ``bundle_location`` (often an agent that got a newer bundle,
                   or an unused duplicate). DB hygiene, not recovery.

This script is intentionally read-only by default; ``--rehydrate`` re-uploads a
single bundle you supply (it does NOT fabricate bytes).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.request


def _http_code(base: str, agent_id: str) -> int:
    req = urllib.request.Request(f"{base}/v1/agents/{agent_id}/image")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return e.code
    except Exception:  # noqa: BLE001
        return 0


async def _present_keys(nats_url: str, bucket: str) -> set[str]:
    import nats

    nc = await nats.connect(nats_url, name="orphan-recovery")
    try:
        obj = await nc.jetstream().object_store(bucket)
        return {o.name for o in (await obj.list())}
    finally:
        await nc.close()


def _db_agents() -> list[tuple[str, str, str, str]]:
    """Return (id, name, sot_tier, bundle_location) from the agents table.

    Read straight from Postgres: ``bundle_location`` is the field we diff
    against the store and the ``/v1/agents`` API does not expose it.
    """
    import psycopg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, coalesce(sot_tier, ''), coalesce(bundle_location, '') "
            "FROM agents ORDER BY name"
        )
        return [tuple(r) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nats-url", default=os.environ.get("OMNIGENT_NATS_URL", "nats://omnigent-nats:4222"))
    ap.add_argument("--bucket", default="omnigent-artifacts")
    ap.add_argument("--base", default=os.environ.get("OMNIGENT_SELF_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--rehydrate", nargs=2, metavar=("AGENT_ID", "BUNDLE"), default=None)
    args = ap.parse_args()

    if args.rehydrate:
        agent_id, bundle = args.rehydrate
        data = open(bundle, "rb").read()  # noqa: SIM115
        req = urllib.request.Request(
            f"{args.base}/v1/agents/{agent_id}/image", data=data, method="PUT",
            headers={"Content-Type": "application/gzip"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"re-uploaded {len(data)} bytes for {agent_id} -> {r.status}")
        return 0

    present = asyncio.run(_present_keys(args.nats_url, args.bucket))
    broken, stale = [], []
    for aid, name, tier, loc in _db_agents():
        if not loc or loc in present:
            continue
        code = _http_code(args.base, aid)
        bucket = broken if code >= 500 else stale
        bucket.append((name, tier, aid, code))

    print(f"present objects: {len(present)}")
    print(f"\nBROKEN (re-upload required — bytes lost): {len(broken)}")
    for name, tier, aid, code in broken:
        print(f"  {name:36s} tier={tier or '-':9s} {aid}  (image {code})")
    print(f"\nSTALE (DB hygiene, not recovery): {len(stale)}")
    for name, tier, aid, code in stale:
        print(f"  {name:36s} tier={tier or '-':9s} {aid}  (image {code})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
