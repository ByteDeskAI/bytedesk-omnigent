from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml


def _server_deployment() -> dict:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/server.yaml").read_text()))
    return next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "omnigent-server"
    )


def _host_docs() -> list[dict]:
    return [
        doc for doc in yaml.safe_load_all(Path("deploy/bytedesk/k8s/host.yaml").read_text()) if doc
    ]


def _host_statefulset() -> dict:
    return next(
        doc
        for doc in _host_docs()
        if doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == "omnigent-host"
    )


def _chief_of_staff_config() -> dict:
    return yaml.safe_load(
        Path("deploy/bytedesk/agents/chief-of-staff/config.yaml").read_text()
    )


def _env(container: dict) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}


def _production_server_patch() -> dict:
    kustomization = yaml.safe_load(
        Path("deploy/bytedesk/fleet/production/kustomization.yaml").read_text()
    )
    raw_patch = next(
        patch["patch"]
        for patch in kustomization["patches"]
        if patch["target"]["kind"] == "Deployment"
        and patch["target"]["name"] == "omnigent-server"
    )
    return yaml.safe_load(raw_patch)


def _rendered_docs(path: str) -> list[dict]:
    rendered = subprocess.run(
        ["kubectl", "kustomize", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def test_rendered_manifests_do_not_store_connector_config_in_secrets_or_configmaps() -> None:
    provider_terms = (
        "connector",
        "google-workspace",
        "google_workspace",
        "atlassian",
        "jira",
        "confluence",
    )

    for path in ("deploy/bytedesk/k8s", "deploy/bytedesk/fleet/production"):
        for doc in _rendered_docs(path):
            if doc.get("kind") not in {"Secret", "ConfigMap"}:
                continue
            metadata = doc.get("metadata") or {}
            payload = {
                "kind": doc.get("kind"),
                "name": metadata.get("name"),
                "labels": metadata.get("labels") or {},
                "annotations": metadata.get("annotations") or {},
                "dataKeys": sorted((doc.get("data") or {}).keys()),
                "stringDataKeys": sorted((doc.get("stringData") or {}).keys()),
            }
            if doc.get("kind") == "ConfigMap":
                payload["dataValues"] = doc.get("data") or {}
                payload["binaryDataKeys"] = sorted((doc.get("binaryData") or {}).keys())
            haystack = json.dumps(payload, sort_keys=True).lower()
            assert not any(term in haystack for term in provider_terms), payload


def test_server_manifest_does_not_store_google_workspace_connector_config() -> None:
    deployment = _server_deployment()
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = _env(container)

    assert pod_spec["serviceAccountName"] == "omnigent-server"
    assert not any(name.startswith("OMNIGENT_GOOGLE_WORKSPACE_") for name in env)
    assert not any("google-workspace" in volume["name"] for volume in pod_spec["volumes"])


def test_host_manifest_exposes_generic_extension_token_request_capability() -> None:
    docs = _host_docs()
    service_account = next(
        doc
        for doc in docs
        if doc["kind"] == "ServiceAccount" and doc["metadata"]["name"] == "omnigent-host"
    )
    role = next(
        doc
        for doc in docs
        if doc["kind"] == "Role" and doc["metadata"]["name"] == "omnigent-host-token-request"
    )
    binding = next(
        doc
        for doc in docs
        if doc["kind"] == "RoleBinding"
        and doc["metadata"]["name"] == "omnigent-host-token-request"
    )
    pod_spec = _host_statefulset()["spec"]["template"]["spec"]

    assert service_account["metadata"]["labels"]["app.kubernetes.io/name"] == "omnigent-host"
    assert pod_spec["serviceAccountName"] == "omnigent-host"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts/token"],
            "resourceNames": ["omnigent-host"],
            "verbs": ["create"],
        }
    ]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "omnigent-host"},
        {"kind": "ServiceAccount", "name": "omnigent-server"},
    ]
    assert binding["roleRef"]["name"] == "omnigent-host-token-request"


def test_production_overlay_does_not_store_google_workspace_connector_config() -> None:
    patch = _production_server_patch()
    pod_spec = patch["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = _env(container)

    assert not any(name.startswith("OMNIGENT_GOOGLE_WORKSPACE_") for name in env)


def test_maya_platform_mcp_no_longer_carries_legacy_google_workspace_tools() -> None:
    allowlist = _chief_of_staff_config()["tools"]["bytedesk-platform"]["tool_allowlist"]

    assert not any(tool.startswith("googleworkspace_") for tool in allowlist)
