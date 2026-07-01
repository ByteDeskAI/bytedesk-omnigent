from __future__ import annotations

from bytedesk_omnigent.extension import BytedeskExtension


def test_bytedesk_extension_mounts_org_mcp_for_every_agent() -> None:
    servers = {server.name: server for server in BytedeskExtension().default_mcp_servers()}

    assert {"memory", "org"} <= set(servers)
    org = servers["org"]
    assert org.transport == "stdio"
    assert org.command == "python"
    assert org.args == ["-m", "bytedesk_omnigent.org_mcp"]
    assert org.tool_allowlist == [
        "get_chart",
        "find_agent",
        "get_effective_access",
    ]


def test_bytedesk_extension_intercepts_org_tools_server_side() -> None:
    interceptors = BytedeskExtension().tool_interceptors()

    assert "org__" in interceptors
    assert interceptors["org__"]("org__unknown", {}, caller_agent_id="ag_maya") is None
