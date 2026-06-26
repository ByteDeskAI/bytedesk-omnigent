from __future__ import annotations

from pathlib import Path

import yaml


def _host_statefulset() -> dict:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/host.yaml").read_text()))
    return next(
        doc
        for doc in docs
        if doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == "omnigent-host"
    )


def _host_startup_script(statefulset: dict) -> str:
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "host"
    return container["args"][0]


def _host_container(statefulset: dict) -> dict:
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "host"
    return container


def test_codex_host_home_is_persistent_source_of_truth() -> None:
    statefulset = _host_statefulset()

    claims = {
        claim["metadata"]["name"]: claim
        for claim in statefulset["spec"].get("volumeClaimTemplates", [])
    }
    pod_volumes = {
        volume["name"] for volume in statefulset["spec"]["template"]["spec"].get("volumes", [])
    }

    assert statefulset["spec"]["replicas"] == 1
    assert "omni-home" in claims
    assert claims["omni-home"]["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert "omni-home" not in pod_volumes


def test_codex_auth_secret_bootstraps_without_overwriting_home_auth() -> None:
    script = _host_startup_script(_host_statefulset())

    existing_home_check = '[ -f "$HOME/.codex/auth.json" ]'
    bootstrap_secret_check = "[ -f /etc/codex-auth/auth.json ]"

    assert existing_home_check in script
    assert bootstrap_secret_check in script
    assert script.index(existing_home_check) < script.index(bootstrap_secret_check)
    assert "using existing codex auth.json from ~/.codex" in script
    assert "bootstrapped codex auth.json into ~/.codex" in script


def test_host_gets_nats_runtime_config_from_infisical_and_passes_it_to_runners() -> None:
    container = _host_container(_host_statefulset())
    env = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}
    secret_refs = {
        entry["secretRef"]["name"]
        for entry in container.get("envFrom", [])
        if "secretRef" in entry
    }

    assert "omnigent-runtime-config-secrets" in secret_refs
    assert "OMNIGENT_NATS_URL" not in env
    passthrough = {name.strip() for name in env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(",")}
    assert "OMNIGENT_NATS_URL" in passthrough
