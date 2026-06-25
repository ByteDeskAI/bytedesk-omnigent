#!/usr/bin/env python3
"""Invariants of the microkernel prototype. Run: ``python3 -m unittest test_prototype``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import omnigent_demo.extensions  # noqa: E402,F401  (self-registration side-effect)
from omnigent_demo import bootstrap  # noqa: E402
from omnigent_demo.core.interfaces import ArtifactStore, Clock  # noqa: E402
from omnigent_demo.core.stores_ext import FakeS3ArtifactStore, InMemoryArtifactStore  # noqa: E402
from omnigent_demo.kernel import Extension, Lifetime  # noqa: E402
from omnigent_demo.kernel.di import Container, DIResolutionError  # noqa: E402
from omnigent_demo.sdk import Host  # noqa: E402  (the SDK Host adds .build())


class SDKContract(unittest.TestCase):
    def test_decorated_class_satisfies_kernel_protocol(self):
        # The SDK facade compiles down to the kernel Extension contract.
        from omnigent_demo.core.stores_ext import StoresExtension

        self.assertIsInstance(StoresExtension(), Extension)


class Lifecycle(unittest.TestCase):
    def test_stages_fire_in_order(self):
        host = bootstrap(discover=True)
        self.assertEqual(
            [p.name for p in host.plugins],
            ["core.stores", "core.tools", "core.harnesses", "bytedesk"],
        )

    def test_missing_dependency_fails_fast(self):
        # core.tools requires core.stores; booting it alone must raise.
        from omnigent_demo.core.tools_ext import ToolsExtension

        with self.assertRaises(LookupError):
            Host.build().with_extension(ToolsExtension()).boot()

    def test_disabled_extension_is_skipped(self):
        host = bootstrap(discover=False, disable=("core.harnesses",))
        self.assertIsNone(host.get_plugin("core.harnesses"))


# Module-level so get_type_hints can resolve annotations (PEP 563 needs module globals).
class _A:
    pass


class _B:
    def __init__(self, a: _A):
        self.a = a


class _Thing:
    pass


class DependencyInjection(unittest.TestCase):
    def test_resolve_by_interface(self):
        host = bootstrap(discover=False)
        self.assertIsInstance(host.resolve(ArtifactStore), InMemoryArtifactStore)

    def test_interface_swap_via_strangler_flag(self, ):
        import os

        os.environ["OMNIGENT_USE_ARTIFACT_STORE"] = "s3"
        try:
            host = bootstrap(discover=False)
            self.assertIsInstance(host.resolve(ArtifactStore), FakeS3ArtifactStore)
        finally:
            del os.environ["OMNIGENT_USE_ARTIFACT_STORE"]

    def test_third_party_replaces_a_core_interface(self):
        # bytedesk re-registers Clock → its impl wins (last registration).
        host = bootstrap(discover=True)
        self.assertEqual(type(host.resolve(Clock)).__name__, "TenantClock")

    def test_constructor_autowiring(self):
        c = Container()
        c.register_type(_A)
        c.register_type(_B)
        self.assertIsInstance(c.resolve(_B).a, _A)

    def test_lifetimes(self):
        c = Container()
        c.register_type(_Thing, lifetime=Lifetime.SINGLETON)
        self.assertIs(c.resolve(_Thing), c.resolve(_Thing))

        c2 = Container()
        c2.register_type(_Thing, lifetime=Lifetime.TRANSIENT)
        self.assertIsNot(c2.resolve(_Thing), c2.resolve(_Thing))

    def test_cycle_is_detected(self):
        c = Container()
        c.register_factory("x", lambda con: con.resolve("x"))
        with self.assertRaises(DIResolutionError):
            c.resolve("x")

    def test_scope_shares_singletons_isolates_scoped(self):
        c = Container()
        c.register_type(_Thing, lifetime=Lifetime.SINGLETON)
        c.register_factory("req", lambda con: object(), lifetime=Lifetime.SCOPED)
        scope = c.create_scope()
        self.assertIs(scope.resolve(_Thing), c.resolve(_Thing))  # singleton shared
        self.assertIsNot(scope.resolve("req"), c.create_scope().resolve("req"))  # scoped isolated


class ToolsUseInjectedDeps(unittest.TestCase):
    def test_tool_gets_injected_store_and_clock(self):
        host = bootstrap(discover=True)
        record = host.seams["tools"].get("record")
        record("k", "v")
        audit = host.seams["tools"].get("audit")
        self.assertIn("k", audit())


if __name__ == "__main__":
    unittest.main(verbosity=2)
