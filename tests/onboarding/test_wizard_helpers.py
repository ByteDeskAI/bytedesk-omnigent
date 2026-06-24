"""Unit tests for pure helpers and non-TTY fallbacks in ``wizard.py``.

The raw-termios interactive paths cannot run in CI, so these tests exercise
the numbered ``click.prompt`` fallbacks and every helper that does not need a
real terminal.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent.onboarding import wizard as wizard_mod
from omnigent.onboarding.wizard import (
    _AgentChoice,
    _GoBack,
    _SupervisorConfig,
    _arrow_menu,
    _arrow_menu_fallback,
    _build_agent_labels,
    _default_agent_name,
    _detect_api_harnesses,
    _detect_coding_agents,
    _find_existing_configs,
    _finish_existing_setup,
    _finish_new_setup,
    _prompt_agent_name,
    _prompt_existing_or_new,
    _prompt_use_case,
    _section,
    _show_welcome,
    _generate_multi_agent_yaml,
    _generate_single_agent_yaml,
    _list_databricks_profiles,
    _prompt_agent_config_path,
    _prompt_cli_supervisor_config,
    _prompt_global_auth,
    _prompt_openai_agents_config,
    _prompt_server_url,
    _prompt_supervisor,
    _sanitize_agent_name,
    _save_yaml,
    _show_coding_agents_and_pick,
    _show_coding_agents_and_pick_multi,
    _show_no_agents_found,
    _store_default_config,
    _text_prompt,
    run_wizard_and_launch,
)


@pytest.fixture()
def non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force wizard helpers onto their non-TTY ``click.prompt`` fallbacks."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        return next(fed)

    monkeypatch.setattr("click.termui.visible_prompt_func", _fake_prompt)


def _feed_hidden(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        return next(fed)

    monkeypatch.setattr("click.termui.hidden_prompt_func", _fake_prompt)


# ── _sanitize_agent_name ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My Agent!", "my_agent"),
        ("  Foo-Bar  ", "foo_bar"),
        ("already_ok", "already_ok"),
        ("___", "my_agent"),
        ("", "my_agent"),
        ("A B C", "a_b_c"),
    ],
)
def test_sanitize_agent_name(raw: str, expected: str) -> None:
    assert _sanitize_agent_name(raw) == expected


# ── _default_agent_name ────────────────────────────────────────────────


def test_default_agent_name_returns_base_when_unused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "agents"
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", agents)
    assert _default_agent_name(1) == "my_coding_agent"
    assert _default_agent_name(2) == "my_coding_team"


def test_default_agent_name_avoids_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "my_coding_agent.yaml").write_text("name: x\n")
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", agents)
    name = _default_agent_name(1)
    assert name.startswith("my_coding_agent_")
    assert not (agents / f"{name}.yaml").exists()


# ── _build_agent_labels ────────────────────────────────────────────────


def test_build_agent_labels_marks_missing_as_disabled() -> None:
    detected = {"claude-sdk": None, "codex": "/usr/bin/codex", "pi": None}
    labels, disabled, order, any_available = _build_agent_labels(detected)
    assert order == ["claude-sdk", "codex", "pi"]
    assert any_available is True
    assert 0 in disabled
    assert 2 in disabled
    assert 1 not in disabled
    assert len(labels) == 3
    assert "found at /usr/bin/codex" in labels[1]


def test_build_agent_labels_all_missing() -> None:
    detected = {"claude-sdk": None, "codex": None, "pi": None}
    _labels, disabled, _order, any_available = _build_agent_labels(detected)
    assert any_available is False
    assert disabled == {0, 1, 2}


# ── YAML generation ────────────────────────────────────────────────────


def test_generate_single_agent_yaml() -> None:
    agent = _AgentChoice(harness="codex", display="Codex")
    yaml = _generate_single_agent_yaml("my_bot", agent)
    assert "name: my_bot" in yaml
    assert "harness: codex" in yaml


def test_generate_multi_agent_yaml_with_task_and_databricks_profile() -> None:
    workers = [
        _AgentChoice(harness="claude-sdk", display="Claude Code"),
        _AgentChoice(harness="codex", display="Codex"),
    ]
    supervisor = _SupervisorConfig(
        harness="openai-agents",
        model="gpt-4o",
        task="Review then fix.",
        profile="DEFAULT",
    )
    yaml = _generate_multi_agent_yaml("team", workers, supervisor)
    assert "Your task: Review then fix." in yaml
    assert "type: databricks" in yaml
    assert "profile: DEFAULT" in yaml
    assert "claude_sdk_worker:" in yaml
    assert "codex_worker:" in yaml


def test_generate_multi_agent_yaml_api_key_auth() -> None:
    workers = [_AgentChoice(harness="codex", display="Codex")]
    supervisor = _SupervisorConfig(
        harness="openai-agents",
        model="gpt-4o",
        api_key="sk-test",
    )
    yaml = _generate_multi_agent_yaml("team", workers, supervisor)
    assert "api_key: $OPENAI_API_KEY" in yaml
    assert "type: api_key" in yaml


def test_generate_multi_agent_yaml_without_task() -> None:
    workers = [_AgentChoice(harness="pi", display="Pi")]
    supervisor = _SupervisorConfig(harness="claude-sdk")
    yaml = _generate_multi_agent_yaml("solo_team", workers, supervisor)
    assert "Your task:" not in yaml
    assert "harness: claude-sdk" in yaml


# ── Detection helpers ──────────────────────────────────────────────────


def test_detect_coding_agents_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wizard_mod.shutil,
        "which",
        lambda name: f"/bin/{name}" if name == "claude" else None,
    )
    detected = _detect_coding_agents()
    assert detected["claude-sdk"] == "/bin/claude"
    assert detected["codex"] is None


def test_detect_api_harnesses_import_success(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = __import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"agents", "google.antigravity"}:
            return MagicMock()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    result = _detect_api_harnesses()
    assert result["openai-agents"] is True
    assert result["antigravity"] is True


def test_detect_api_harnesses_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_import(name: str, *args: Any, **kwargs: Any) -> Any:
        raise ImportError(name)

    monkeypatch.setattr("builtins.__import__", _fail_import)
    result = _detect_api_harnesses()
    assert result == {"openai-agents": False, "antigravity": False}


def test_list_databricks_profiles_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _list_databricks_profiles() == []


def test_list_databricks_profiles_parses_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text("[profile-a]\nhost = https://a\n[profile-b]\nhost = https://b\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _list_databricks_profiles() == ["profile-a", "profile-b"]


def test_list_databricks_profiles_invalid_cfg_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text("not valid ini {{{")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _list_databricks_profiles() == []


def test_list_databricks_profiles_defaults_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text("[DEFAULT]\nhost = https://default\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _list_databricks_profiles() == ["DEFAULT"]


# ── _find_existing_configs / _save_yaml ──────────────────────────────


def test_find_existing_configs_newest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    old = agents / "old.yaml"
    new = agents / "new.yaml"
    old.write_text("name: old\n")
    new.write_text("name: new\n")
    old.touch()
    new.touch()
    # Ensure mtime ordering: new is newer.
    import os
    import time

    now = time.time()
    os.utime(old, (now - 10, now - 10))
    os.utime(new, (now, now))
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", agents)
    configs = _find_existing_configs()
    assert [p.name for p in configs] == ["new.yaml", "old.yaml"]


def test_find_existing_configs_empty_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", tmp_path / "missing")
    assert _find_existing_configs() == []


def test_save_yaml_writes_under_agents_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "agents"
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", agents)
    path = _save_yaml("name: demo\n", "demo.yaml")
    assert path == agents / "demo.yaml"
    assert path.read_text() == "name: demo\n"


# ── Non-TTY fallbacks ────────────────────────────────────────────────


def test_arrow_menu_fallback_single_choice(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(monkeypatch, ["2"])
    result = _arrow_menu_fallback(["alpha", "beta", "gamma"])
    assert result == 1


def test_arrow_menu_fallback_reprompts_invalid(
    non_tty: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, ["9", "1"])
    result = _arrow_menu_fallback(["only"])
    assert result == 0
    assert "Invalid selection." in capsys.readouterr().out


def test_arrow_menu_fallback_multi_select(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(monkeypatch, ["1,3"])
    result = _arrow_menu_fallback(["a", "b", "c"], multi=True)
    assert result == [0, 2]


def test_arrow_menu_fallback_multi_skips_disabled(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(monkeypatch, ["1,3"])
    result = _arrow_menu_fallback(["a", "b", "c"], disabled={1}, multi=True)
    assert result == [0, 2]


def test_text_prompt_non_tty_returns_value(non_tty: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, ["hello"])
    assert _text_prompt("Label", default="fallback") == "hello"


def test_text_prompt_non_tty_empty_without_default_raises_goback(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(monkeypatch, ["   "])
    with pytest.raises(_GoBack):
        _text_prompt("Label", default=None)


def test_text_prompt_non_tty_hidden_input(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed_hidden(monkeypatch, ["secret"])
    assert _text_prompt("Key", hide_input=True) == "secret"


# ── Display helpers ──────────────────────────────────────────────────


def test_show_no_agents_found_prints_labels(capsys: pytest.CaptureFixture[str]) -> None:
    _show_no_agents_found(["missing claude", "missing codex"])
    out = capsys.readouterr().out
    assert "missing claude" in out
    assert "No coding agents found" in out


def test_prompt_cli_supervisor_config() -> None:
    cfg = _prompt_cli_supervisor_config("codex")
    assert cfg.harness == "codex"
    assert cfg.model is None


# ── Mocked interactive flows ─────────────────────────────────────────


def test_show_coding_agents_and_pick_none_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detected = {"claude-sdk": None, "codex": None, "pi": None}
    assert _show_coding_agents_and_pick(detected) is None
    assert "No coding agents found" in capsys.readouterr().out


def test_show_coding_agents_and_pick_returns_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    detected = {"claude-sdk": "/bin/claude", "codex": None, "pi": None}
    choice = _show_coding_agents_and_pick(detected)
    assert choice == _AgentChoice(harness="codex", display="Codex")


def test_show_coding_agents_and_pick_multi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: [0, 2])
    detected = {"claude-sdk": "/bin/claude", "codex": None, "pi": "/bin/pi"}
    selected = _show_coding_agents_and_pick_multi(detected)
    assert selected is not None
    assert len(selected) == 2
    assert selected[0].harness == "claude-sdk"
    assert selected[1].harness == "pi"


def test_prompt_server_url_blank_when_no_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "")
    assert _prompt_server_url(None) is None


def test_prompt_server_url_keeps_current_on_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_goback(*_a: Any, **_k: Any) -> str:
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_text_prompt", _raise_goback)
    assert _prompt_server_url("https://existing") == "https://existing"


def test_prompt_server_url_returns_typed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "https://new")
    assert _prompt_server_url("https://old") == "https://new"


def test_prompt_agent_config_path_valid_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = tmp_path / "agent.yaml"
    agent.write_text("name: x\n")
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: str(agent))
    assert _prompt_agent_config_path() == agent


def test_prompt_agent_config_path_reprompts_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = iter([str(tmp_path / "nope.yaml"), str(tmp_path / "ok.yaml")])

    def _prompt(*_a: Any, **_k: Any) -> str:
        return next(calls)

    ok = tmp_path / "ok.yaml"
    ok.write_text("name: ok\n")
    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    assert _prompt_agent_config_path() == ok
    assert "not exist" in capsys.readouterr().out.replace("\n", " ")


def test_prompt_agent_config_path_rejects_non_yaml_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "agent.txt"
    bad.write_text("x")
    good = tmp_path / "good.yml"
    good.write_text("name: g\n")
    calls = iter([str(bad), str(good)])

    def _prompt(*_a: Any, **_k: Any) -> str:
        return next(calls)

    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    assert _prompt_agent_config_path() == good
    assert ".yaml or .yml" in capsys.readouterr().out


def test_prompt_agent_config_path_empty_raises_goback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "   ")
    with pytest.raises(_GoBack):
        _prompt_agent_config_path()


def test_store_default_config_writes_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(
        "omnigent.cli._save_global_config",
        lambda settings: saved.append(settings),
    )
    yaml_path = tmp_path / "agent.yaml"
    supervisor = _SupervisorConfig(harness="openai-agents", profile="prod")
    _store_default_config(yaml_path, supervisor=supervisor)
    assert saved[0]["default_agent"] == str(yaml_path)
    assert saved[0]["auth"] == {"type": "databricks", "profile": "prod"}
    assert "stored default_agent" in capsys.readouterr().out


def test_store_default_config_api_key_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._save_global_config", lambda s: saved.append(s))
    supervisor = _SupervisorConfig(harness="openai-agents", api_key="sk-x")
    _store_default_config(tmp_path / "a.yaml", supervisor=supervisor)
    assert saved[0]["auth"] == {"type": "api_key", "api_key": "$OPENAI_API_KEY"}


def test_finish_new_setup_calls_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stored: list[tuple[Path, _SupervisorConfig | None]] = []

    def _capture(path: Path, *, supervisor: _SupervisorConfig | None = None) -> None:
        stored.append((path, supervisor))

    monkeypatch.setattr(wizard_mod, "_store_default_config", _capture)
    yaml_path = tmp_path / "demo.yaml"
    content = "name: demo\n"
    sup = _SupervisorConfig(harness="codex")
    _finish_new_setup(yaml_path, content, supervisor=sup)
    assert stored == [(yaml_path, sup)]
    assert "Agent config preview" in capsys.readouterr().out


def test_finish_existing_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stored: list[Path] = []
    monkeypatch.setattr(
        wizard_mod,
        "_store_default_config",
        lambda path, supervisor=None: stored.append(path),
    )
    yaml_path = tmp_path / "existing.yaml"
    _finish_existing_setup(yaml_path)
    assert stored == [yaml_path]
    assert "Selected:" in capsys.readouterr().out


def test_prompt_global_auth_api_key_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda label, **_k: "sk-key" if "API" in label else "https://custom")
    auth, _ = _prompt_global_auth()
    assert auth == {"type": "api_key", "api_key": "sk-key", "base_url": "https://custom"}


def test_prompt_global_auth_databricks_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: ["DEFAULT"])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "DEFAULT")
    auth, _ = _prompt_global_auth()
    assert auth == {"type": "databricks", "profile": "DEFAULT"}


def test_prompt_global_auth_escape_at_top_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])

    def _menu(*_a: Any, **_k: Any) -> int:
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    auth, _ = _prompt_global_auth()
    assert auth is None


def test_prompt_global_auth_empty_api_key_retries_then_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    menu_calls = 0

    def _menu(*_a: Any, **_k: Any) -> int:
        nonlocal menu_calls
        menu_calls += 1
        if menu_calls == 1:
            return 0
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "")
    auth, _ = _prompt_global_auth()
    assert auth is None


def test_prompt_openai_agents_openai_api_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda label, **_k: "sk-new" if "API" in label else "gpt-4o")
    cfg = _prompt_openai_agents_config()
    assert cfg.harness == "openai-agents"
    assert cfg.api_key == "sk-new"
    assert cfg.model == "gpt-4o"


def test_prompt_openai_agents_custom_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "databricks-gpt-5-4")
    cfg = _prompt_openai_agents_config()
    assert cfg.base_url == "https://gateway/v1"
    assert cfg.api_key is None
    assert cfg.model == "databricks-gpt-5-4"


def test_prompt_openai_agents_databricks_single_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: ["only"])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 2)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "databricks-gpt-5-4")
    cfg = _prompt_openai_agents_config()
    assert cfg.profile == "only"
    assert cfg.model == "databricks-gpt-5-4"


def test_prompt_supervisor_cli_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    steps = iter(["coordinate", 0, None])

    def _text(*_a: Any, **_k: Any) -> str:
        return next(steps)  # type: ignore[return-value]

    monkeypatch.setattr(wizard_mod, "_text_prompt", _text)
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        wizard_mod,
        "_detect_coding_agents",
        lambda: {"claude-sdk": "/bin/claude", "codex": None, "pi": None},
    )
    monkeypatch.setattr(wizard_mod, "_detect_api_harnesses", lambda: {"openai-agents": False, "antigravity": False})
    monkeypatch.setattr(wizard_mod, "_prompt_cli_supervisor_config", lambda h: _SupervisorConfig(harness=h))
    cfg = _prompt_supervisor({"claude-sdk": "/bin/claude", "codex": None, "pi": None})
    assert cfg.harness == "claude-sdk"
    assert cfg.task == "coordinate"


def test_prompt_supervisor_openai_agents_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "task text")
    # First arrow_menu: harness pick → openai-agents index 3 (after 3 CLI options).
    menus = iter([3])

    def _menu(*_a: Any, **_k: Any) -> int:
        return next(menus)

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    monkeypatch.setattr(wizard_mod, "_detect_api_harnesses", lambda: {"openai-agents": True, "antigravity": False})
    expected = _SupervisorConfig(harness="openai-agents", model="gpt-4o", task="inner")
    monkeypatch.setattr(wizard_mod, "_prompt_openai_agents_config", lambda: expected)
    detected = {"claude-sdk": "/bin/claude", "codex": None, "pi": None}
    cfg = _prompt_supervisor(detected)
    assert cfg.harness == "openai-agents"
    assert cfg.task == "task text"


def test_prompt_supervisor_no_harnesses_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "task")
    monkeypatch.setattr(wizard_mod, "_detect_api_harnesses", lambda: {"openai-agents": False, "antigravity": False})
    detected = {"claude-sdk": None, "codex": None, "pi": None}
    with pytest.raises(SystemExit):
        _prompt_supervisor(detected)


def test_run_wizard_and_launch_persists_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text("name: demo\n")
    saved: list[dict[str, Any]] = []

    monkeypatch.setattr(wizard_mod, "_show_welcome", lambda: None)
    monkeypatch.setattr(wizard_mod, "_section", lambda: None)
    monkeypatch.setattr(wizard_mod, "_prompt_server_url", lambda _c: "https://server")
    monkeypatch.setattr(wizard_mod, "_prompt_global_auth", lambda: ({"type": "api_key", "api_key": "sk"}, None))
    monkeypatch.setattr("omnigent.cli._load_global_config", lambda: {})
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("omnigent.cli._save_global_config", lambda s: saved.append(s))
    monkeypatch.setattr(
        wizard_mod,
        "_text_prompt",
        lambda *_a, **_k: str(agent_yaml),
    )

    run_wizard_and_launch()
    assert saved[0]["server"] == "https://server"
    assert saved[0]["auth"]["type"] == "api_key"
    assert saved[0]["default_agent"] == str(agent_yaml.resolve())
    assert "Config saved" in capsys.readouterr().out


def test_run_wizard_and_launch_keeps_existing_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(wizard_mod, "_show_welcome", lambda: None)
    monkeypatch.setattr(wizard_mod, "_section", lambda: None)
    monkeypatch.setattr(wizard_mod, "_prompt_server_url", lambda _c: None)
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    monkeypatch.setattr("omnigent.cli._load_global_config", lambda: {"auth": {"type": "databricks", "profile": "p"}})
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("omnigent.cli._save_global_config", lambda s: saved.append(s))
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "")

    run_wizard_and_launch()
    assert saved == []
    assert "No changes" in capsys.readouterr().out


def test_arrow_menu_non_tty_delegates_to_fallback(
    non_tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(monkeypatch, ["1"])
    assert _arrow_menu(["only"]) == 0


def test_arrow_menu_fallback_single_value_error_reprompts(
    non_tty: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, ["nope", "1"])
    assert _arrow_menu_fallback(["x"]) == 0
    assert "Invalid selection." in capsys.readouterr().out


def test_arrow_menu_fallback_multi_value_error_reprompts(
    non_tty: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, ["bad", "1"])
    assert _arrow_menu_fallback(["a", "b"], multi=True) == [0]
    assert "Invalid selection." in capsys.readouterr().out


def test_section_prints_separator(capsys: pytest.CaptureFixture[str]) -> None:
    _section()
    assert capsys.readouterr().out.strip() != ""


def test_show_welcome_renders_banner(capsys: pytest.CaptureFixture[str]) -> None:
    _show_welcome()
    out = capsys.readouterr().out
    assert "Omnigent is a declarative" in out
    assert "Welcome" in out or "Omnigent" in out


def test_prompt_use_case_maps_menu_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 2)
    assert _prompt_use_case() == 2


def test_prompt_agent_name_sanitizes_and_avoids_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "taken.yaml").write_text("name: taken\n")
    monkeypatch.setattr(wizard_mod, "_AGENTS_DIR", agents)
    prompts = iter(["taken", "fresh_name"])

    def _prompt(*_a: Any, **_k: Any) -> str:
        return next(prompts)

    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    assert _prompt_agent_name(1) == "fresh_name"
    assert "already exists" in capsys.readouterr().out.replace("\n", " ")


def test_prompt_existing_or_new_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    assert _prompt_existing_or_new([Path("/tmp/a.yaml")]) is None


def test_prompt_existing_or_new_pick_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = [Path("/tmp/first.yaml"), Path("/tmp/second.yaml")]
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    assert _prompt_existing_or_new(configs) == configs[1]


def test_prompt_existing_or_new_type_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = tmp_path / "custom.yaml"
    agent.write_text("name: c\n")
    menus = iter([1, 1])

    def _menu(*_a: Any, **_k: Any) -> int:
        return next(menus)

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    monkeypatch.setattr(wizard_mod, "_prompt_agent_config_path", lambda: agent)
    assert _prompt_existing_or_new([tmp_path / "other.yaml"]) == agent


def test_prompt_existing_or_new_goback_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def _menu(*_a: Any, **_k: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        if calls == 2:
            raise _GoBack
        return 0

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    assert _prompt_existing_or_new([Path("/tmp/a.yaml")]) is None


def test_prompt_global_auth_with_many_profiles_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [f"p{i}" for i in range(5)])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "p0")
    auth, _ = _prompt_global_auth()
    assert auth == {"type": "databricks", "profile": "p0"}


def test_prompt_global_auth_empty_profile_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: ["DEFAULT"])
    menu_calls = 0

    def _menu(*_a: Any, **_k: Any) -> int:
        nonlocal menu_calls
        menu_calls += 1
        if menu_calls == 1:
            return 1
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "")
    auth, _ = _prompt_global_auth()
    assert auth is None


def test_prompt_global_auth_goback_from_credentials_substep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    menu_calls = 0

    def _menu(*_a: Any, **_k: Any) -> int:
        nonlocal menu_calls
        menu_calls += 1
        if menu_calls == 1:
            return 0
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)

    def _prompt(label: str, **_k: Any) -> str:
        if "API" in label:
            raise _GoBack
        return ""

    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    auth, _ = _prompt_global_auth()
    assert auth is None


def test_prompt_openai_agents_with_env_key_skips_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 0)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "gpt-4o")
    cfg = _prompt_openai_agents_config()
    assert cfg.api_key is None
    assert cfg.model == "gpt-4o"


def test_prompt_openai_agents_custom_endpoint_prompts_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: [])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    prompts = iter(["https://gateway/v1", "sk-new", "custom-model"])

    def _prompt(label: str, **_k: Any) -> str:
        return next(prompts)

    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    cfg = _prompt_openai_agents_config()
    assert cfg.base_url == "https://gateway/v1"
    assert cfg.api_key == "sk-new"
    assert cfg.model == "custom-model"


def test_prompt_openai_agents_databricks_multi_profile_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    menus = iter([2, 1])

    def _menu(*_a: Any, **_k: Any) -> int:
        return next(menus)

    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: ["a", "b"])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", _menu)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: "databricks-gpt-5-4")
    cfg = _prompt_openai_agents_config()
    assert cfg.profile == "b"
    assert cfg.model == "databricks-gpt-5-4"


def test_prompt_openai_agents_goback_at_top_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard_mod, "_list_databricks_profiles", lambda: ["p"])
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: (_ for _ in ()).throw(_GoBack()))
    with pytest.raises(_GoBack):
        _prompt_openai_agents_config()


def test_prompt_supervisor_goback_at_task_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wizard_mod,
        "_text_prompt",
        lambda *_a, **_k: (_ for _ in ()).throw(_GoBack()),
    )
    with pytest.raises(_GoBack):
        _prompt_supervisor({"claude-sdk": "/bin/claude", "codex": None, "pi": None})


def test_prompt_agent_config_path_rejects_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.yaml"
    good.write_text("name: ok\n")
    calls = iter([str(tmp_path), str(good)])

    def _prompt(*_a: Any, **_k: Any) -> str:
        return next(calls)

    monkeypatch.setattr(wizard_mod, "_text_prompt", _prompt)
    assert _prompt_agent_config_path() == good
    assert "not a file" in capsys.readouterr().out.replace("\n", " ")


def test_prompt_server_url_no_current_escape_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: Any, **_k: Any) -> str:
        raise _GoBack

    monkeypatch.setattr(wizard_mod, "_text_prompt", _raise)
    assert _prompt_server_url(None) is None


def test_run_wizard_and_launch_reconfigures_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(wizard_mod, "_show_welcome", lambda: None)
    monkeypatch.setattr(wizard_mod, "_section", lambda: None)
    monkeypatch.setattr(wizard_mod, "_prompt_server_url", lambda _c: None)
    monkeypatch.setattr(wizard_mod, "_arrow_menu", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        wizard_mod,
        "_prompt_global_auth",
        lambda: ({"type": "databricks", "profile": "new"}, None),
    )
    monkeypatch.setattr("omnigent.cli._load_global_config", lambda: {"auth": {"type": "api_key"}})
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("omnigent.cli._save_global_config", lambda s: saved.append(s))
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: (_ for _ in ()).throw(_GoBack()))

    run_wizard_and_launch()
    assert saved[0]["auth"] == {"type": "databricks", "profile": "new"}


def test_run_wizard_and_launch_shows_existing_agent_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard_mod, "_show_welcome", lambda: None)
    monkeypatch.setattr(wizard_mod, "_section", lambda: None)
    monkeypatch.setattr(wizard_mod, "_prompt_server_url", lambda _c: None)
    monkeypatch.setattr(wizard_mod, "_prompt_global_auth", lambda: (None, None))
    monkeypatch.setattr(
        "omnigent.cli._load_global_config",
        lambda: {"default_agent": str(tmp_path / "existing.yaml")},
    )
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("omnigent.cli._save_global_config", lambda _s: None)
    monkeypatch.setattr(wizard_mod, "_text_prompt", lambda *_a, **_k: (_ for _ in ()).throw(_GoBack()))

    run_wizard_and_launch()
    assert "Default agent already set" in capsys.readouterr().out