"""SDK DI ``Container`` tests (BDP-2508).

Ports the prototype's container behaviour: SINGLETON / TRANSIENT / SCOPED
lifetimes, constructor auto-wiring, method injection, interface registration,
child scopes, and cycle detection.
"""

from __future__ import annotations

import pytest

from omnigent.sdk import Container, DIResolutionError, Lifetime


class Clock:
    pass


class SystemClock(Clock):
    pass


def test_register_instance_and_resolve():
    c = Container()
    inst = SystemClock()
    c.register_instance(Clock, inst)
    assert c.resolve(Clock) is inst


def test_singleton_is_same_instance():
    c = Container()
    c.register_type(SystemClock, lifetime=Lifetime.SINGLETON)
    assert c.resolve(SystemClock) is c.resolve(SystemClock)


def test_transient_is_fresh_each_time():
    c = Container()
    c.register_type(SystemClock, lifetime=Lifetime.TRANSIENT)
    assert c.resolve(SystemClock) is not c.resolve(SystemClock)


def test_scoped_is_one_per_scope():
    c = Container()
    c.register_type(SystemClock, lifetime=Lifetime.SCOPED)
    a1 = c.resolve(SystemClock)
    a2 = c.resolve(SystemClock)
    assert a1 is a2  # same scope -> same
    child = c.create_scope()
    b1 = child.resolve(SystemClock)
    assert b1 is not a1  # different scope -> different


def test_interface_registration_dependency_inversion():
    c = Container()
    c.register_type(SystemClock, key=Clock)  # depend on the interface
    resolved = c.resolve(Clock)
    assert isinstance(resolved, SystemClock)


def test_constructor_autowiring():
    class Service:
        def __init__(self, clock: Clock):
            self.clock = clock

    c = Container()
    c.register_type(SystemClock, key=Clock)
    c.register_type(Service)
    svc = c.resolve(Service)
    assert isinstance(svc.clock, SystemClock)


def test_method_injection_via_call():
    c = Container()
    c.register_type(SystemClock, key=Clock)

    def build(clock: Clock):
        return ("built", clock)

    label, clock = c.call(build)
    assert label == "built" and isinstance(clock, SystemClock)


def test_call_leaves_defaulted_unresolvable_params_alone():
    c = Container()

    class Unregistered:
        pass

    def build(missing: Unregistered = None):
        return missing

    # Unregistered type with a default -> left alone (not injected, not an error).
    assert c.call(build) is None


def test_unregistered_raises():
    c = Container()
    with pytest.raises(DIResolutionError):
        c.resolve(Clock)


def test_try_resolve_returns_default():
    c = Container()
    assert c.try_resolve(Clock, default="fallback") == "fallback"


def test_cycle_detection():
    c = Container()
    c.register_factory(Clock, lambda cc: cc.resolve(Clock), lifetime=Lifetime.TRANSIENT)
    with pytest.raises(DIResolutionError):
        c.resolve(Clock)


def test_child_scope_inherits_singletons():
    c = Container()
    c.register_type(SystemClock, key=Clock, lifetime=Lifetime.SINGLETON)
    parent_inst = c.resolve(Clock)
    child = c.create_scope()
    assert child.resolve(Clock) is parent_inst  # singleton shared down the scope tree
