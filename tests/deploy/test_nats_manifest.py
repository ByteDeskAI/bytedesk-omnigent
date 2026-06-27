from __future__ import annotations

import json
from pathlib import Path

import yaml


def _docs() -> list[dict]:
    return [
        doc for doc in yaml.safe_load_all(Path("deploy/bytedesk/k8s/nats.yaml").read_text()) if doc
    ]


def _nats_ui_docs() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(Path("deploy/bytedesk/k8s/nats-ui.yaml").read_text())
        if doc
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


def test_nats_ui_autoloads_consolidated_nats_context() -> None:
    docs = {doc["kind"]: doc for doc in _nats_ui_docs()}
    secret = docs["Secret"]
    service = docs["Service"]
    statefulset = docs["StatefulSet"]

    assert service["metadata"]["name"] == "omnigent-nats-ui"
    assert service["spec"]["type"] == "ClusterIP"
    assert not any(doc["kind"] == "Ingress" for doc in _nats_ui_docs())

    context = json.loads(secret["stringData"]["omnigent.json"])
    assert context["url"] == "nats://omnigent-nats:4222"
    assert context["user"] == ""
    assert context["password"] == ""

    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].startswith("ghcr.io/nats-nui/nui@sha256:")
    assert "--nats-cli-contexts=/cli-contexts" in container["args"]

    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["cli-contexts"]["mountPath"] == "/cli-contexts"
    assert str(mounts["cli-contexts"]["readOnly"]).lower() == "true"
    assert mounts["db-volume"]["mountPath"] == "/db"

    volumes = {
        volume["name"]: volume
        for volume in statefulset["spec"]["template"]["spec"].get("volumes", [])
    }
    assert volumes["cli-contexts"]["secret"]["secretName"] == "omnigent-nats-ui-contexts"

    claim = statefulset["spec"]["volumeClaimTemplates"][0]
    assert claim["metadata"]["name"] == "db-volume"
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"


def test_kustomization_includes_nats_ui() -> None:
    kustomization = yaml.safe_load(Path("deploy/bytedesk/k8s/kustomization.yaml").read_text())

    assert "nats-ui.yaml" in kustomization["resources"]


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
