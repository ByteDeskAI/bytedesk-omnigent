"""Batch-22 coverage for package ``__init__`` re-exports and tiny module gaps."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import bytedesk_omnigent.auth as auth
import bytedesk_omnigent.auth.principal_resolver as auth_principal_resolver
import bytedesk_omnigent.bus as bus
import bytedesk_omnigent.bus.signal_bus as signal_bus
import bytedesk_omnigent.release as release
import bytedesk_omnigent.release.orchestrator as release_orchestrator
import bytedesk_omnigent.scheduler as scheduler
import bytedesk_omnigent.scheduler.loop as scheduler_loop
import bytedesk_omnigent.scheduler.scheduler as cron_scheduler
import bytedesk_omnigent.sessions as sessions
import bytedesk_omnigent.sessions.initiate as sessions_initiate
import bytedesk_omnigent.tasks as tasks
import bytedesk_omnigent.tasks.store as tasks_store
import bytedesk_omnigent.tool_steps as tool_steps
import bytedesk_omnigent.tool_steps.store as tool_steps_store
import omnigent.client_tools.async_demo as async_demo
import omnigent.environments as environments
import omnigent.inner.bwrap_sandbox as inner_bwrap
import omnigent.inner.datamodel as inner_dm
import omnigent.inner.os_env as inner_os_env
import omnigent.inner.sandbox as inner_sandbox
import omnigent.repl as repl
import omnigent.repl._repl as repl_impl
import omnigent.sandbox as sandbox
import omnigent.sandbox.bwrap as bwrap
from bytedesk_omnigent import lifecycle
from omnigent.client_tools import get_tool_set


def _assert_all_symbols_importable(module: object, package_name: str) -> None:
    for name in module.__all__:  # type: ignore[attr-defined]
        assert hasattr(module, name), f"{package_name} missing re-export {name!r}"


# ── bytedesk_omnigent/auth/__init__.py ───────────────────────────────────────


def test_auth_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(auth, "bytedesk_omnigent.auth")


def test_auth_package_reexports_principal_resolver_objects() -> None:
    assert auth.HEADER_NAME is auth_principal_resolver.HEADER_NAME
    assert auth.SECRET_ENV is auth_principal_resolver.SECRET_ENV
    assert auth.ByteDeskPrincipalResolver is auth_principal_resolver.ByteDeskPrincipalResolver
    assert auth.map_capabilities_to_roles is auth_principal_resolver.map_capabilities_to_roles


# ── bytedesk_omnigent/bus/__init__.py ────────────────────────────────────────


def test_bus_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(bus, "bytedesk_omnigent.bus")


def test_bus_package_reexports_signal_bus_and_lifecycle_objects() -> None:
    assert bus.DeliveryResult is signal_bus.DeliveryResult
    assert bus.DeliveryStatus is signal_bus.DeliveryStatus
    assert bus.PendingWait is signal_bus.PendingWait
    assert bus.SqlAlchemySignalBus is signal_bus.SqlAlchemySignalBus
    assert bus.WaitKind is lifecycle.WaitKind
    assert bus.WaitStatus is lifecycle.WaitStatus


# ── bytedesk_omnigent/release/__init__.py ────────────────────────────────────


def test_release_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(release, "bytedesk_omnigent.release")


def test_release_package_reexports_orchestrator_objects() -> None:
    assert release.HumanGatedReleaseExecutor is release_orchestrator.HumanGatedReleaseExecutor
    assert release.ReleaseExecutor is release_orchestrator.ReleaseExecutor
    assert release.ReleaseOrchestrator is release_orchestrator.ReleaseOrchestrator
    assert release.ReleaseParkResult is release_orchestrator.ReleaseParkResult
    assert release.ReleaseTriggerResult is release_orchestrator.ReleaseTriggerResult
    assert release.release_signal_id is release_orchestrator.release_signal_id


# ── bytedesk_omnigent/scheduler/__init__.py ───────────────────────────────────


def test_scheduler_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(scheduler, "bytedesk_omnigent.scheduler")


def test_scheduler_package_reexports_loop_and_scheduler_objects() -> None:
    assert scheduler.cron_scheduler_loop is scheduler_loop.cron_scheduler_loop
    assert scheduler.CronTrigger is cron_scheduler.CronTrigger
    assert scheduler.SqlAlchemyCronScheduler is cron_scheduler.SqlAlchemyCronScheduler
    assert scheduler.compute_next_fire is cron_scheduler.compute_next_fire
    assert scheduler.register_schedule_kind is cron_scheduler.register_schedule_kind
    assert scheduler.run_cron_scheduler_tick is cron_scheduler.run_cron_scheduler_tick


# ── bytedesk_omnigent/sessions/__init__.py ───────────────────────────────────


def test_sessions_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(sessions, "bytedesk_omnigent.sessions")


def test_sessions_package_reexports_initiate_objects() -> None:
    assert sessions.HttpSelfCallInitiator is sessions_initiate.HttpSelfCallInitiator
    assert sessions.SessionInitiator is sessions_initiate.SessionInitiator
    assert sessions.build_cron_dispatch is sessions_initiate.build_cron_dispatch
    assert sessions.build_self_call_initiator_from_env is sessions_initiate.build_self_call_initiator_from_env
    assert sessions.get_session_initiator is sessions_initiate.get_session_initiator
    assert sessions.set_session_initiator is sessions_initiate.set_session_initiator


# ── bytedesk_omnigent/tasks/__init__.py ──────────────────────────────────────


def test_tasks_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(tasks, "bytedesk_omnigent.tasks")


def test_tasks_package_reexports_store_and_lifecycle_objects() -> None:
    assert tasks.SqlAlchemyTaskStore is tasks_store.SqlAlchemyTaskStore
    assert tasks.Task is tasks_store.Task
    assert tasks.TaskStore is tasks_store.TaskStore
    assert tasks.get_task_store is tasks_store.get_task_store
    assert tasks.sql_task_to_entity is tasks_store.sql_task_to_entity
    assert tasks.WorkflowLifecycleStatus is lifecycle.WorkflowLifecycleStatus


# ── bytedesk_omnigent/tool_steps/__init__.py ─────────────────────────────────


def test_tool_steps_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(tool_steps, "bytedesk_omnigent.tool_steps")


def test_tool_steps_package_reexports_store_and_lifecycle_objects() -> None:
    assert tool_steps.SqlAlchemyToolStepStore is tool_steps_store.SqlAlchemyToolStepStore
    assert tool_steps.StepClaim is tool_steps_store.StepClaim
    assert tool_steps.StepOutcome is tool_steps_store.StepOutcome
    assert tool_steps.ToolStep is tool_steps_store.ToolStep
    assert tool_steps.ToolStepBusy is tool_steps_store.ToolStepBusy
    assert tool_steps.ToolStepExhausted is tool_steps_store.ToolStepExhausted
    assert tool_steps.run_tool_step is tool_steps_store.run_tool_step
    assert tool_steps.StepStatus is lifecycle.StepStatus


# ── omnigent/environments/__init__.py ────────────────────────────────────────


def test_environments_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(environments, "omnigent.environments")


def test_environments_package_reexports_inner_objects() -> None:
    assert environments.OSEnvironment is inner_os_env.OSEnvironment
    assert environments.CallerProcessOSEnvironment is inner_os_env.CallerProcessOSEnvironment
    assert environments.create_os_environment is inner_os_env.create_os_environment
    assert environments.default_os_env_spec_for_type is inner_os_env.default_os_env_spec_for_type
    assert environments.OSEnvSpec is inner_dm.OSEnvSpec
    assert environments.OSEnvSandboxSpec is inner_dm.OSEnvSandboxSpec


# ── omnigent/sandbox/__init__.py ─────────────────────────────────────────────


def test_sandbox_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(sandbox, "omnigent.sandbox")


def test_sandbox_package_reexports_inner_objects() -> None:
    assert sandbox.SandboxPolicy is inner_sandbox.SandboxPolicy
    assert sandbox.SandboxBackend is inner_sandbox.SandboxBackend
    assert sandbox.resolve_sandbox is inner_sandbox.resolve_sandbox
    assert sandbox.activate_sandbox is inner_sandbox.activate_sandbox
    assert sandbox.register_backend is inner_sandbox.register_backend
    assert sandbox.get_backend is inner_sandbox.get_backend
    assert sandbox.with_additional_read_roots is inner_sandbox.with_additional_read_roots
    assert sandbox.with_additional_write_files is inner_sandbox.with_additional_write_files
    assert sandbox.with_additional_write_roots is inner_sandbox.with_additional_write_roots
    assert sandbox.with_denied_unix_sockets is inner_sandbox.with_denied_unix_sockets
    assert sandbox.with_spawn_env_allowlist is inner_sandbox.with_spawn_env_allowlist
    assert sandbox.create_private_tmpdir is inner_sandbox.create_private_tmpdir
    assert sandbox.cleanup_private_tmpdir is inner_sandbox.cleanup_private_tmpdir
    assert sandbox.set_temp_env is inner_sandbox.set_temp_env
    assert sandbox.run_launcher is inner_sandbox.run_launcher
    assert sandbox.create_exec_launcher is inner_sandbox.create_exec_launcher


# ── omnigent/sandbox/bwrap.py ────────────────────────────────────────────────


def test_bwrap_module_all_symbols_importable() -> None:
    _assert_all_symbols_importable(bwrap, "omnigent.sandbox.bwrap")


def test_bwrap_module_reexports_inner_backend() -> None:
    assert bwrap.BwrapSandboxBackend is inner_bwrap.BwrapSandboxBackend


def test_bwrap_import_triggers_backend_registration() -> None:
    backend = inner_sandbox._get_backend("linux_bwrap")
    assert isinstance(backend, bwrap.BwrapSandboxBackend)


# ── omnigent/repl/__init__.py ────────────────────────────────────────────────


def test_repl_package_all_symbols_importable() -> None:
    _assert_all_symbols_importable(repl, "omnigent.repl")


def test_repl_package_reexports_repl_impl_objects() -> None:
    assert repl.register_skill_commands is repl_impl.register_skill_commands
    assert repl.run_repl is repl_impl.run_repl
    assert repl.unregister_skill_commands is repl_impl.unregister_skill_commands


# ── omnigent/client_tools/async_demo.py ──────────────────────────────────────


def test_async_demo_tool_set_loads_via_registry() -> None:
    tool_set = get_tool_set("async_demo")
    assert tool_set is async_demo
    assert len(tool_set.TOOLS) == 1
    assert tool_set.TOOLS[0]["function"]["name"] == "slow_compute"


def test_async_demo_execute_tool_slow_compute_with_mocked_sleep() -> None:
    with patch("omnigent.client_tools.async_demo.time.sleep") as sleep:
        result = async_demo.execute_tool(
            "slow_compute",
            {"seconds": 2.5, "label": "demo-label"},
        )
    sleep.assert_called_once_with(2.5)
    assert result == "finished 'demo-label' after 2.5s"


def test_async_demo_execute_tool_unknown_name_raises_key_error() -> None:
    with pytest.raises(KeyError, match="async_demo only exports 'slow_compute'"):
        async_demo.execute_tool("unknown_tool", {"seconds": 1.0, "label": "x"})