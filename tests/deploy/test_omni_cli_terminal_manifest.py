from __future__ import annotations

from pathlib import Path

import yaml


def test_omni_cli_exec_role_supports_websocket_handshake() -> None:
    docs = list(yaml.safe_load_all(Path("deploy/bytedesk/k8s/omni-cli-terminal.yaml").read_text()))
    role = next(
        doc
        for doc in docs
        if doc["kind"] == "Role" and doc["metadata"]["name"] == "omnigent-cli-exec"
    )

    exec_rule = next(rule for rule in role["rules"] if rule["resources"] == ["pods/exec"])

    assert exec_rule["resourceNames"] == ["omnigent-cli-0"]
    assert set(exec_rule["verbs"]) == {"get", "create"}
