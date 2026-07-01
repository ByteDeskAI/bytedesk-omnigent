"""Pin ap-web connectorPresentation.ts group tables to the Python manifests.

The UI's service grouping tables are hand-maintained; a manifest service key
missing from them silently falls into the "Other services" bucket. This test
makes that drift loud in CI instead.
"""

import re
from pathlib import Path

from bytedesk_omnigent.connectors.manifests import bytedesk_connector_manifests

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESENTATION = REPO_ROOT / "ap-web" / "src" / "lib" / "connectorPresentation.ts"

GROUP_TABLES = {
    "google_workspace": "GOOGLE_WORKSPACE_GROUPS",
    "atlassian": "ATLASSIAN_GROUPS",
}


def _grouped_service_keys(source: str, table_name: str) -> set[str]:
    match = re.search(
        rf"const {table_name}: GroupDefinition\[\] = \[(.*?)\n\];", source, re.S
    )
    assert match, f"{table_name} table not found in connectorPresentation.ts"
    keys: set[str] = set()
    for services_array in re.findall(r"services:\s*\[(.*?)\]", match.group(1), re.S):
        keys.update(re.findall(r'"([^"]+)"', services_array))
    return keys


def test_presentation_group_tables_cover_manifest_services() -> None:
    source = PRESENTATION.read_text(encoding="utf-8")
    manifests = {manifest.provider: manifest for manifest in bytedesk_connector_manifests()}
    assert set(manifests) == set(GROUP_TABLES), (
        "connector provider set drifted — add a grouping table in "
        "connectorPresentation.ts and register it in GROUP_TABLES"
    )
    for provider, table_name in GROUP_TABLES.items():
        grouped = _grouped_service_keys(source, table_name)
        manifest_keys = {service.key for service in manifests[provider].services}
        missing = manifest_keys - grouped
        assert not missing, (
            f"{provider} services missing from {table_name} "
            f"(they would fall into 'Other services'): {sorted(missing)}"
        )
        stale = grouped - manifest_keys
        assert not stale, (
            f"{table_name} lists service keys unknown to the {provider} manifest: "
            f"{sorted(stale)}"
        )
