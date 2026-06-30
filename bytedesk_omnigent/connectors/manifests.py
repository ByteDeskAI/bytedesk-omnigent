"""Connector manifest contract and first-party manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AuthType = Literal["oauth_3lo", "google_domain_wide_delegation"]
SetupFieldInput = Literal["text", "json_secret"]
SetupFieldTarget = Literal["metadata", "secret_payload"]


@dataclass(frozen=True)
class ConnectorSetupField:
    """One provider-specific setup input rendered by the shared admin UI."""

    key: str
    label: str
    target: SetupFieldTarget = "metadata"
    input: SetupFieldInput = "text"
    required: bool = True
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "target": self.target,
            "input": self.input,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True)
class ConnectorAuthSpec:
    """How an external provider is connected."""

    type: AuthType
    scopes: list[str] = field(default_factory=list)
    docs_url: str | None = None
    setup_fields: list[ConnectorSetupField] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "scopes": list(self.scopes),
            "docsUrl": self.docs_url,
            "setupFields": [field.to_dict() for field in self.setup_fields],
        }


@dataclass(frozen=True)
class ConnectorTool:
    """One connector-managed tool that can be granted independently."""

    key: str
    name: str
    description: str
    mcp_tool: str
    scopes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "mcpTool": self.mcp_tool,
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class ConnectorService:
    """One service inside a provider connector."""

    key: str
    name: str
    description: str
    scopes: list[str] = field(default_factory=list)
    tool_mounts: list[str] = field(default_factory=list)
    tools: list[ConnectorTool] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "scopes": list(self.scopes),
            "toolMounts": list(self.tool_mounts),
            "tools": [tool.to_dict() for tool in self.tools],
        }


@dataclass(frozen=True)
class ConnectorManifest:
    """Provider/service metadata contributed by an Omnigent extension."""

    provider: str
    name: str
    description: str
    auth: ConnectorAuthSpec
    services: list[ConnectorService]
    docs_url: str | None = None

    def service(self, key: str) -> ConnectorService | None:
        return next((svc for svc in self.services if svc.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "description": self.description,
            "auth": self.auth.to_dict(),
            "services": [svc.to_dict() for svc in self.services],
            "docsUrl": self.docs_url,
        }


ATLASSIAN_SCOPES = [
    "read:jira-work",
    "write:jira-work",
    "read:confluence-content.all",
    "write:confluence-content",
]

GOOGLE_WORKSPACE_OPERATION_DEFINITIONS = {
    "read": ("Read", ["GET"]),
    "search": ("Search", ["GET", "POST"]),
    "create": ("Create", ["POST"]),
    "update": ("Update", ["PATCH", "PUT", "POST"]),
    "batch_update": ("Batch update", ["POST"]),
    "delete": ("Delete", ["DELETE"]),
    "send": ("Send", ["POST"]),
    "share": ("Share", ["POST", "PATCH", "PUT"]),
    "settings": ("Manage settings", ["GET", "PATCH", "PUT"]),
    "execute": ("Execute", ["GET", "POST"]),
    "admin_read": ("Admin read", ["GET"]),
    "admin_mutate": ("Admin mutate", ["POST", "PATCH", "PUT", "DELETE"]),
    "generate": ("Generate", ["POST"]),
}

GOOGLE_WORKSPACE_SERVICE_CATALOG = {
    "workspace": {
        "service_id": "workspace",
        "name": "Workspace",
        "base_url": "",
        "scopes": [],
        "operations": [],
    },
    "gmail": {
        "service_id": "gmail",
        "name": "Gmail",
        "base_url": "https://gmail.googleapis.com",
        "scopes": ["https://mail.google.com/"],
        "operations": ["read", "search", "create", "update", "send", "settings"],
    },
    "calendar": {
        "service_id": "calendar",
        "name": "Calendar",
        "base_url": "https://www.googleapis.com/calendar/v3",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "chat": {
        "service_id": "chat",
        "name": "Chat",
        "base_url": "https://chat.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/chat.messages",
            "https://www.googleapis.com/auth/chat.spaces",
        ],
        "operations": ["read", "search", "create", "update", "delete", "send"],
    },
    "drive": {
        "service_id": "drive",
        "name": "Drive",
        "base_url": "https://www.googleapis.com/drive/v3",
        "scopes": ["https://www.googleapis.com/auth/drive"],
        "operations": ["read", "search", "create", "update", "delete", "share"],
    },
    "docs": {
        "service_id": "docs",
        "name": "Docs",
        "base_url": "https://docs.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/documents"],
        "operations": ["read", "create", "update", "batch_update"],
    },
    "sheets": {
        "service_id": "sheets",
        "name": "Sheets",
        "base_url": "https://sheets.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
        "operations": ["read", "create", "update", "batch_update"],
    },
    "slides": {
        "service_id": "slides",
        "name": "Slides",
        "base_url": "https://slides.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/presentations"],
        "operations": ["read", "create", "update", "batch_update"],
    },
    "forms": {
        "service_id": "forms",
        "name": "Forms",
        "base_url": "https://forms.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/forms.body",
            "https://www.googleapis.com/auth/forms.responses.readonly",
        ],
        "operations": ["read", "create", "update"],
    },
    "keep": {
        "service_id": "keep",
        "name": "Keep",
        "base_url": "https://keep.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/keep"],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "meet": {
        "service_id": "meet",
        "name": "Meet",
        "base_url": "https://meet.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/meetings.space.created",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        ],
        "operations": ["read", "create", "update"],
    },
    "sites": {
        "service_id": "sites",
        "name": "Sites",
        "base_url": "https://sites.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/sites"],
        "operations": ["read", "search", "create", "update"],
    },
    "tasks": {
        "service_id": "tasks",
        "name": "Tasks",
        "base_url": "https://tasks.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/tasks"],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "admin_settings": {
        "service_id": "admin-settings",
        "name": "Admin Settings",
        "base_url": "https://admin.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/admin.directory.domain"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "admin_directory": {
        "service_id": "admin-directory",
        "name": "Admin Directory",
        "base_url": "https://admin.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/admin.directory.user",
            "https://www.googleapis.com/auth/admin.directory.group",
            "https://www.googleapis.com/auth/admin.directory.orgunit",
            "https://www.googleapis.com/auth/admin.directory.domain",
        ],
        "operations": ["admin_read", "admin_mutate"],
    },
    "cloud_identity": {
        "service_id": "cloud-identity",
        "name": "Cloud Identity",
        "base_url": "https://cloudidentity.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/cloud-identity.groups",
            "https://www.googleapis.com/auth/cloud-identity.devices",
        ],
        "operations": ["admin_read", "admin_mutate"],
    },
    "people": {
        "service_id": "people",
        "name": "People",
        "base_url": "https://people.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/contacts",
            "https://www.googleapis.com/auth/directory.readonly",
        ],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "domain_shared_contacts": {
        "service_id": "domain-shared-contacts",
        "name": "Domain Shared Contacts",
        "base_url": "https://www.google.com/m8/feeds",
        "scopes": ["https://www.google.com/m8/feeds/contacts/"],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "contact_delegation": {
        "service_id": "contact-delegation",
        "name": "Contact Delegation",
        "base_url": "https://admin.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/admin.directory.user"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "groups_settings": {
        "service_id": "groups-settings",
        "name": "Groups Settings",
        "base_url": "https://groupssettings.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/apps.groups.settings"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "groups_migration": {
        "service_id": "groups-migration",
        "name": "Groups Migration",
        "base_url": "https://groupsmigration.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/apps.groups.migration"],
        "operations": ["create"],
    },
    "license_manager": {
        "service_id": "license-manager",
        "name": "License Manager",
        "base_url": "https://licensing.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/apps.licensing"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "reports": {
        "service_id": "reports",
        "name": "Reports",
        "base_url": "https://admin.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/admin.reports.audit.readonly",
            "https://www.googleapis.com/auth/admin.reports.usage.readonly",
        ],
        "operations": ["admin_read", "search"],
    },
    "alert_center": {
        "service_id": "alert-center",
        "name": "Alert Center",
        "base_url": "https://alertcenter.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/apps.alerts"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "data_transfer": {
        "service_id": "data-transfer",
        "name": "Data Transfer",
        "base_url": "https://admin.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/admin.datatransfer"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "reseller": {
        "service_id": "reseller",
        "name": "Reseller",
        "base_url": "https://reseller.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/apps.order"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "cloud_search": {
        "service_id": "cloud-search",
        "name": "Cloud Search",
        "base_url": "https://cloudsearch.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/cloud_search.query",
            "https://www.googleapis.com/auth/cloud_search",
        ],
        "operations": ["read", "search"],
    },
    "drive_activity": {
        "service_id": "drive-activity",
        "name": "Drive Activity",
        "base_url": "https://driveactivity.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/drive.activity"],
        "operations": ["read", "search"],
    },
    "drive_labels": {
        "service_id": "drive-labels",
        "name": "Drive Labels",
        "base_url": "https://drivelabels.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/drive.labels",
            "https://www.googleapis.com/auth/drive.labels.readonly",
        ],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "apps_script": {
        "service_id": "apps-script",
        "name": "Apps Script",
        "base_url": "https://script.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/script.projects",
            "https://www.googleapis.com/auth/script.deployments",
            "https://www.googleapis.com/auth/script.processes",
        ],
        "operations": ["read", "search", "create", "update", "delete", "execute"],
    },
    "workspace_add_ons": {
        "service_id": "workspace-add-ons",
        "name": "Workspace Add-ons",
        "base_url": "https://script.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/script.projects",
            "https://www.googleapis.com/auth/script.deployments",
        ],
        "operations": ["read", "create", "update", "delete"],
    },
    "drive_apps": {
        "service_id": "drive-apps",
        "name": "Drive Apps",
        "base_url": "https://www.googleapis.com/drive/v3",
        "scopes": [
            "https://www.googleapis.com/auth/drive.apps.readonly",
            "https://www.googleapis.com/auth/drive.appdata",
        ],
        "operations": ["read", "search", "create", "update", "delete"],
    },
    "marketplace": {
        "service_id": "marketplace",
        "name": "Workspace Marketplace",
        "base_url": "https://appsmarket.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/appsmarketplace.license"],
        "operations": ["read", "search", "admin_read"],
    },
    "gmail_settings": {
        "service_id": "gmail-settings",
        "name": "Gmail Settings",
        "base_url": "https://gmail.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.settings.basic",
            "https://www.googleapis.com/auth/gmail.settings.sharing",
        ],
        "operations": ["read", "settings"],
    },
    "email_audit": {
        "service_id": "email-audit",
        "name": "Email Audit",
        "base_url": "https://apps-apis.google.com/a/feeds/compliance/audit",
        "scopes": ["https://apps-apis.google.com/a/feeds/compliance/audit/"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "postmaster_tools": {
        "service_id": "postmaster-tools",
        "name": "Postmaster Tools",
        "base_url": "https://gmailpostmastertools.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/postmaster.readonly"],
        "operations": ["read", "search"],
    },
    "chrome_browser_cloud_management": {
        "service_id": "chrome-browser-cloud-management",
        "name": "Chrome Browser Cloud Management",
        "base_url": "https://chromemanagement.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/chrome.management.appdetails.readonly",
            "https://www.googleapis.com/auth/chrome.management.policy",
            "https://www.googleapis.com/auth/chrome.management.reports.readonly",
        ],
        "operations": ["admin_read", "admin_mutate"],
    },
    "chrome_enrollment_tokens": {
        "service_id": "chrome-enrollment-tokens",
        "name": "Chrome Enrollment Tokens",
        "base_url": "https://admin.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/admin.directory.device.chromeos"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "chrome_printer_management": {
        "service_id": "chrome-printer-management",
        "name": "Chrome Printer Management",
        "base_url": "https://admin.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/admin.chrome.printers"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "vault": {
        "service_id": "vault",
        "name": "Vault",
        "base_url": "https://vault.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/ediscovery"],
        "operations": ["admin_read", "admin_mutate"],
    },
    "vertex_ai": {
        "service_id": "vertex-ai",
        "name": "Vertex AI",
        "base_url": "https://aiplatform.googleapis.com",
        "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        "operations": ["read", "generate"],
    },
}

GOOGLE_WORKSPACE_SERVICES = [
    (key, spec["name"]) for key, spec in GOOGLE_WORKSPACE_SERVICE_CATALOG.items()
]

GOOGLE_WORKSPACE_CURATED_ACTIONS = {
    "workspace": [
        (
            "services_list",
            "List services",
            "List Google Workspace services available to agents.",
            "services_list",
        ),
        (
            "capabilities_get",
            "Get capabilities",
            "Return capability metadata for a Google Workspace service.",
            "capabilities_get",
        ),
        (
            "subject_resolve",
            "Resolve subject",
            "Resolve the delegated Workspace subject for a request.",
            "subject_resolve",
        ),
        (
            "audit_query",
            "Query audit",
            "Query connector-visible Google Workspace audit rows.",
            "audit_query",
        ),
    ],
    "docs": [
        ("create", "Create document", "Create a Google Doc.", "docs_create"),
        (
            "batch_update",
            "Batch update document",
            "Apply Google Docs batchUpdate requests.",
            "docs_batch_update",
        ),
        (
            "template_merge",
            "Merge template",
            "Copy and merge a Google Docs template.",
            "docs_template_merge",
        ),
        (
            "template_seed",
            "Seed template",
            "Create a reusable Google Docs template.",
            "docs_template_seed",
        ),
        (
            "templates_list",
            "List templates",
            "List configured Google Docs templates.",
            "docs_templates_list",
        ),
    ],
    "sheets": [
        ("create", "Create spreadsheet", "Create a Google Sheet.", "sheets_create"),
        (
            "values_update",
            "Update values",
            "Update a Google Sheets range.",
            "sheets_values_update",
        ),
    ],
    "slides": [
        ("create", "Create presentation", "Create a Google Slides presentation.", "slides_create"),
    ],
    "forms": [
        ("create", "Create form", "Create a Google Form.", "forms_create"),
    ],
    "drive": [
        (
            "share_internal",
            "Share internally",
            "Share a Drive file with the Workspace domain.",
            "drive_share_internal",
        ),
        (
            "replicate_template",
            "Replicate template",
            "Copy a Drive template file.",
            "drive_replicate_template",
        ),
        ("search", "Search Drive", "Search Google Drive files.", "drive_search"),
        (
            "file_create",
            "Create file metadata",
            "Create a Drive file or folder metadata record.",
            "drive_file_create",
        ),
        (
            "file_copy",
            "Copy file",
            "Copy a Drive file with Shared Drive support.",
            "drive_file_copy",
        ),
    ],
    "gmail": [
        ("draft_create", "Create draft", "Create a Gmail draft.", "gmail_draft_create"),
        ("thread_read", "Read thread", "Read a Gmail thread.", "gmail_thread_read"),
        ("search", "Search Gmail", "Search Gmail messages.", "gmail_search"),
        (
            "send_internal",
            "Send internal message",
            "Send an internal Gmail message.",
            "gmail_send_internal",
        ),
    ],
    "calendar": [
        (
            "event_create",
            "Create event",
            "Create a Google Calendar event.",
            "calendar_event_create",
        ),
        (
            "freebusy",
            "Free/busy query",
            "Query Calendar free/busy availability.",
            "calendar_freebusy",
        ),
        (
            "meeting_schedule",
            "Schedule meeting",
            "Create a Calendar event with conferencing.",
            "meeting_schedule",
        ),
    ],
    "meet": [
        ("space_create", "Create Meet space", "Create a Google Meet space.", "meet_space_create"),
    ],
    "chat": [
        (
            "send_internal",
            "Send Chat message",
            "Send a Google Chat message to an internal space.",
            "chat_send_internal",
        ),
    ],
    "people": [
        ("search", "Search people", "Search Google People contacts.", "people_search"),
    ],
    "admin_directory": [
        (
            "user_get",
            "Get directory user",
            "Read a Google Workspace Directory user.",
            "directory_user_get",
        ),
    ],
    "tasks": [
        ("create", "Create task", "Create a Google Task.", "tasks_create"),
    ],
    "keep": [
        ("note_create", "Create note", "Create a Google Keep note.", "keep_note_create"),
    ],
}

GOOGLE_WORKSPACE_SERVICE_SCOPES = {
    key: list(spec["scopes"]) for key, spec in GOOGLE_WORKSPACE_SERVICE_CATALOG.items()
}

GOOGLE_WORKSPACE_TOOL_SCOPES = {
    "docs_template_merge": [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ],
    "drive_replicate_template": ["https://www.googleapis.com/auth/drive"],
}


def google_workspace_operation_tool_name(service_key: str, operation_key: str) -> str:
    return f"{service_key}_{operation_key}"


def google_workspace_service_id(service_key: str) -> str:
    spec = GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]
    return str(spec["service_id"])


def google_workspace_base_url(service_key: str) -> str:
    spec = GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]
    return str(spec["base_url"])


def google_workspace_operation_methods(operation_key: str) -> list[str]:
    return list(GOOGLE_WORKSPACE_OPERATION_DEFINITIONS[operation_key][1])


def google_workspace_all_scopes() -> list[str]:
    scopes: list[str] = []
    seen: set[str] = set()
    for spec in GOOGLE_WORKSPACE_SERVICE_CATALOG.values():
        for scope in spec["scopes"]:
            if scope not in seen:
                scopes.append(scope)
                seen.add(scope)
    return scopes


def _google_workspace_operation_action(
    service_key: str,
    operation_key: str,
) -> tuple[str, str, str, str]:
    spec = GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]
    label = GOOGLE_WORKSPACE_OPERATION_DEFINITIONS[operation_key][0]
    service_name = str(spec["name"])
    service_id = str(spec["service_id"])
    return (
        operation_key,
        f"{label} {service_name}",
        (
            f"{label} Google Workspace {service_name} resources through the "
            f"structured {service_id} {operation_key} adapter."
        ),
        google_workspace_operation_tool_name(service_key, operation_key),
    )


def _google_workspace_actions_for_service(service_key: str) -> list[tuple[str, str, str, str]]:
    curated = list(GOOGLE_WORKSPACE_CURATED_ACTIONS.get(service_key, []))
    existing_tool_keys = {action[0] for action in curated}
    existing_mcp_tools = {action[3] for action in curated}
    for operation_key in GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]["operations"]:
        action = _google_workspace_operation_action(service_key, operation_key)
        if action[0] in existing_tool_keys or action[3] in existing_mcp_tools:
            continue
        curated.append(action)
    return curated


GOOGLE_WORKSPACE_ACTIONS = {
    key: _google_workspace_actions_for_service(key) for key in GOOGLE_WORKSPACE_SERVICE_CATALOG
}


def atlassian_connector_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        provider="atlassian",
        name="Atlassian",
        description="Jira and Confluence tools using one Atlassian Cloud OAuth connection.",
        auth=ConnectorAuthSpec(
            type="oauth_3lo",
            scopes=ATLASSIAN_SCOPES,
            docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
            setup_fields=[
                ConnectorSetupField(
                    key="auth_mode",
                    label="Auth mode",
                    required=False,
                    description="Optional connector credential adapter, for example api_token.",
                ),
                ConnectorSetupField(
                    key="base_url_secret",
                    label="Site URL secret name",
                    required=False,
                    description="Omnigent secret name containing the Atlassian site URL.",
                ),
                ConnectorSetupField(
                    key="email_secret",
                    label="Email secret name",
                    required=False,
                    description="Omnigent secret name containing the Atlassian account email.",
                ),
                ConnectorSetupField(
                    key="api_token_secret",
                    label="API token secret name",
                    required=False,
                    description="Omnigent secret name containing the Atlassian API token.",
                ),
            ],
        ),
        services=[
            ConnectorService(
                key="jira",
                name="Jira",
                description="Search, read, create, comment on, and transition Jira issues.",
                scopes=["read:jira-work", "write:jira-work"],
                tool_mounts=["atlassian:jira"],
                tools=[
                    ConnectorTool(
                        key="search",
                        name="Search issues",
                        description="Search Jira issues with JQL.",
                        mcp_tool="jira_search",
                        scopes=["read:jira-work"],
                    ),
                    ConnectorTool(
                        key="get_issue",
                        name="Read issue",
                        description="Read a Jira issue by key.",
                        mcp_tool="jira_get_issue",
                        scopes=["read:jira-work"],
                    ),
                    ConnectorTool(
                        key="add_comment",
                        name="Add comment",
                        description="Add a plain-text comment to a Jira issue.",
                        mcp_tool="jira_add_comment",
                        scopes=["write:jira-work"],
                    ),
                    ConnectorTool(
                        key="transition",
                        name="Transition issue",
                        description="Move a Jira issue through an available workflow transition.",
                        mcp_tool="jira_transition",
                        scopes=["write:jira-work"],
                    ),
                    ConnectorTool(
                        key="create_issue",
                        name="Create issue",
                        description="Create a Jira issue in a project.",
                        mcp_tool="jira_create_issue",
                        scopes=["write:jira-work"],
                    ),
                ],
            ),
            ConnectorService(
                key="confluence",
                name="Confluence",
                description="Search, read, create, update, and comment on Confluence pages.",
                scopes=["read:confluence-content.all", "write:confluence-content"],
                tool_mounts=["atlassian:confluence"],
                tools=[
                    ConnectorTool(
                        key="search",
                        name="Search pages",
                        description="Search Confluence pages with CQL.",
                        mcp_tool="confluence_search",
                        scopes=["read:confluence-content.all"],
                    ),
                    ConnectorTool(
                        key="get_page",
                        name="Read page",
                        description="Read a Confluence page by id.",
                        mcp_tool="confluence_get_page",
                        scopes=["read:confluence-content.all"],
                    ),
                    ConnectorTool(
                        key="create_page",
                        name="Create page",
                        description="Create a Confluence page.",
                        mcp_tool="confluence_create_page",
                        scopes=["write:confluence-content"],
                    ),
                    ConnectorTool(
                        key="update_page",
                        name="Update page",
                        description="Update a Confluence page using storage-format content.",
                        mcp_tool="confluence_update_page",
                        scopes=["write:confluence-content"],
                    ),
                    ConnectorTool(
                        key="add_comment",
                        name="Add page comment",
                        description="Add a footer comment to a Confluence page.",
                        mcp_tool="confluence_add_comment",
                        scopes=["write:confluence-content"],
                    ),
                ],
            ),
        ],
        docs_url="https://developer.atlassian.com/cloud/",
    )


def google_workspace_connector_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        provider="google_workspace",
        name="Google Workspace",
        description="Google Workspace service tools using domain-wide delegated service auth.",
        auth=ConnectorAuthSpec(
            type="google_domain_wide_delegation",
            scopes=google_workspace_all_scopes(),
            docs_url="https://developers.google.com/workspace/guides/create-credentials",
            setup_fields=[
                ConnectorSetupField(
                    key="domain",
                    label="Workspace domain",
                    required=False,
                    description="Optional domain label for operators.",
                ),
                ConnectorSetupField(
                    key="delegated_subject",
                    label="Delegated subject",
                    description="Admin user to impersonate with domain-wide delegation.",
                ),
                ConnectorSetupField(
                    key="service_account_email",
                    label="Service account email",
                    required=False,
                    description="Keyless Workload Identity service account email.",
                ),
                ConnectorSetupField(
                    key="workload_identity_token_source",
                    label="Workload identity token source",
                    required=False,
                    description=(
                        "Optional: file or kubernetes_token_request. Blank uses file "
                        "when a token file is set, otherwise Kubernetes TokenRequest."
                    ),
                ),
                ConnectorSetupField(
                    key="workload_identity_token_file",
                    label="Workload identity token file",
                    required=False,
                    description="Projected Kubernetes token file for keyless auth.",
                ),
                ConnectorSetupField(
                    key="workload_identity_audience",
                    label="Workload identity audience",
                    required=False,
                    description="Google Workload Identity Federation audience.",
                ),
                ConnectorSetupField(
                    key="service_account_json",
                    label="Service account JSON",
                    target="secret_payload",
                    input="json_secret",
                    required=False,
                    description="Google service account credential JSON.",
                ),
            ],
        ),
        services=[
            ConnectorService(
                key=key,
                name=name,
                description=f"Expose Google Workspace {name} tools to assigned agents.",
                tool_mounts=[f"mcp__google__{key}"],
                scopes=list(GOOGLE_WORKSPACE_SERVICE_SCOPES[key]),
                tools=[
                    ConnectorTool(
                        key=tool_key,
                        name=tool_name,
                        description=tool_description,
                        mcp_tool=mcp_tool,
                        scopes=list(
                            GOOGLE_WORKSPACE_TOOL_SCOPES.get(
                                mcp_tool,
                                GOOGLE_WORKSPACE_SERVICE_SCOPES[key],
                            )
                        ),
                    )
                    for tool_key, tool_name, tool_description, mcp_tool in (
                        GOOGLE_WORKSPACE_ACTIONS[key]
                    )
                ],
            )
            for key, name in GOOGLE_WORKSPACE_SERVICES
        ],
        docs_url="https://developers.google.com/workspace",
    )


def bytedesk_connector_manifests() -> list[ConnectorManifest]:
    return [atlassian_connector_manifest(), google_workspace_connector_manifest()]
