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


def _server_docs() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(Path("deploy/bytedesk/k8s/server.yaml").read_text())
        if doc
    ]


def _server_env_from_secret_refs() -> set[str]:
    docs = _server_docs()
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


def test_single_consolidated_nats_instance() -> None:
    """One NATS instance hosts coordination + artifacts (BDP-2585).

    The dedicated omnigent-nats-artifacts StatefulSet/Service was consolidated
    away; omnigent-nats is the only instance and its PVC grew to absorb the
    artifact Object Store.
    """
    statefulsets = {
        doc["metadata"]["name"]: doc for doc in _docs() if doc["kind"] == "StatefulSet"
    }
    services = {doc["metadata"]["name"]: doc for doc in _docs() if doc["kind"] == "Service"}

    assert "omnigent-nats" in statefulsets
    assert "omnigent-nats" in services
    # The dedicated artifact instance must be gone.
    assert "omnigent-nats-artifacts" not in statefulsets
    assert "omnigent-nats-artifacts" not in services

    claim = statefulsets["omnigent-nats"]["spec"]["volumeClaimTemplates"][0]
    # Claim-template name is immutable; it stays "jetstream-data".
    assert claim["metadata"]["name"] == "jetstream-data"
    # Sized to hold both coordination KV (tiny) and the artifact Object Store.
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_server_points_artifacts_at_consolidated_nats() -> None:
    env = _server_env()

    assert env["ARTIFACT_DIR"] == ("nats://omnigent-nats:4222/omnigent-artifacts")


def test_server_reads_nats_runtime_config_from_infisical_secret() -> None:
    env = _server_env()
    secret_refs = _server_env_from_secret_refs()

    assert "OMNIGENT_NATS_URL" not in env
    assert "omnigent-runtime-config-secrets" in secret_refs


def test_server_manifest_removes_peer_forwarding_service() -> None:
    services = {
        doc["metadata"]["name"]
        for doc in _server_docs()
        if doc["kind"] == "Service"
    }

    assert "omnigent-server" in services
    assert "omnigent-server-peer" not in services


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
