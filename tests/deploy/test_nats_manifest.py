from __future__ import annotations

from pathlib import Path

import yaml


def _docs() -> list[dict]:
    return [
        doc for doc in yaml.safe_load_all(Path("deploy/bytedesk/k8s/nats.yaml").read_text()) if doc
    ]


def _server_env() -> dict[str, str]:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/server.yaml").read_text()))
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "omnigent-server"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}


def _server_env_from_secret_refs() -> set[str]:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/server.yaml").read_text()))
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "omnigent-server"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {
        entry["secretRef"]["name"]
        for entry in container.get("envFrom", [])
        if "secretRef" in entry
    }


def _runtime_config_infisical_secret() -> dict:
    docs = list(
        yaml.safe_load_all(Path("deploy/bytedesk/k8s/omnigent-runtime-config-secret.yaml").read_text())
    )
    return next(doc for doc in docs if doc["kind"] == "InfisicalSecret")


def test_coordination_and_artifact_nats_are_isolated() -> None:
    """Artifacts must not share the coordination NATS store_dir/PVC."""
    statefulsets = {
        doc["metadata"]["name"]: doc for doc in _docs() if doc["kind"] == "StatefulSet"
    }
    services = {doc["metadata"]["name"]: doc for doc in _docs() if doc["kind"] == "Service"}

    assert {"omnigent-nats", "omnigent-nats-artifacts"} <= set(statefulsets)
    assert {"omnigent-nats", "omnigent-nats-artifacts"} <= set(services)

    coord_claims = {
        claim["metadata"]["name"]
        for claim in statefulsets["omnigent-nats"]["spec"]["volumeClaimTemplates"]
    }
    artifact_claims = {
        claim["metadata"]["name"]
        for claim in statefulsets["omnigent-nats-artifacts"]["spec"]["volumeClaimTemplates"]
    }
    # Existing StatefulSets already use this claim-template name; changing it
    # makes kubectl apply fail because volumeClaimTemplates are immutable.
    assert coord_claims == {"jetstream-data"}
    assert artifact_claims == {"artifact-jetstream-data"}


def test_server_points_artifacts_at_dedicated_nats() -> None:
    env = _server_env()

    assert env["ARTIFACT_DIR"] == ("nats://omnigent-nats-artifacts:4222/omnigent-artifacts")


def test_server_reads_nats_runtime_config_from_infisical_secret() -> None:
    env = _server_env()
    secret_refs = _server_env_from_secret_refs()

    assert "OMNIGENT_NATS_URL" not in env
    assert "omnigent-runtime-config-secrets" in secret_refs


def test_nats_runtime_config_secret_is_infisical_managed() -> None:
    secret = _runtime_config_infisical_secret()
    scope = secret["spec"]["authentication"]["universalAuth"]["secretsScope"]
    managed = secret["spec"]["managedSecretReference"]

    assert scope["projectSlug"] == "bytedesk-agent-configuration"
    assert scope["envSlug"] == "development"
    assert scope["secretsPath"] == "/runtime"
    assert managed["secretName"] == "omnigent-runtime-config-secrets"


def test_config_artifact_location_matches_server_env() -> None:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/config.yaml").read_text()))
    config = next(doc for doc in docs if doc["kind"] == "ConfigMap")
    server_env = _server_env()

    payload = yaml.safe_load(config["data"]["config.yaml"])
    assert payload["artifact_location"] == server_env["ARTIFACT_DIR"]
