"""Tests for the website package built-in tool."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from bytedesk_omnigent.tools.website_package_tools import BytedeskPackageWebsiteZipTool
from omnigent.tools.base import ToolContext


def _ctx(workspace: Path, conversation_id: str | None = "conv_website_1") -> ToolContext:
    return ToolContext(
        task_id="task_website_1",
        agent_id="ag_web_dev",
        workspace=workspace,
        conversation_id=conversation_id,
    )


def _patch_stores(monkeypatch) -> tuple[list[dict[str, Any]], list[tuple[str, bytes]]]:
    stored_files: list[dict[str, Any]] = []
    stored_artifacts: list[tuple[str, bytes]] = []

    class _FakeFileRecord:
        def __init__(self, file_id: str) -> None:
            self.id = file_id

    class _FakeFileStore:
        def create(
            self,
            filename: str,
            bytes: int,
            content_type: str,
            session_id: str | None = None,
        ) -> _FakeFileRecord:
            stored_files.append(
                {
                    "filename": filename,
                    "bytes": bytes,
                    "content_type": content_type,
                    "session_id": session_id,
                }
            )
            return _FakeFileRecord("file_site_zip_123")

    class _FakeArtifactStore:
        def put(self, key: str, data: bytes) -> None:
            stored_artifacts.append((key, data))

    monkeypatch.setattr("omnigent.runtime.get_file_store", lambda: _FakeFileStore())
    monkeypatch.setattr("omnigent.runtime.get_artifact_store", lambda: _FakeArtifactStore())
    return stored_files, stored_artifacts


def _write_site(workspace: Path) -> None:
    static = workspace / "dist"
    source = workspace / "src-site"
    (static / "assets").mkdir(parents=True)
    source.mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Acme</title>", encoding="utf-8")
    (static / "assets" / "styles.css").write_text("body { color: #111; }", encoding="utf-8")
    (source / "index.html").write_text("<main>editable</main>", encoding="utf-8")
    (source / "README.md").write_text("source notes\n", encoding="utf-8")


def test_package_website_zip_stores_static_and_source_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_site(workspace)
    stored_files, stored_artifacts = _patch_stores(monkeypatch)

    result = json.loads(
        BytedeskPackageWebsiteZipTool().invoke(
            json.dumps(
                {
                    "static_dir": "dist",
                    "source_dir": "src-site",
                    "filename": "Acme Landing",
                    "asset_file_ids": ["file_img_hero"],
                }
            ),
            _ctx(workspace),
        )
    )

    assert result["ok"] is True
    assert result["file_id"] == "file_site_zip_123"
    assert result["session_id"] == "conv_website_1"
    assert result["filename"] == "Acme-Landing.zip"
    assert result["content_type"] == "application/zip"
    assert result["download_url"] == (
        "/v1/sessions/conv_website_1/resources/files/file_site_zip_123/content"
    )
    assert stored_files == [
        {
            "filename": "Acme-Landing.zip",
            "bytes": result["bytes"],
            "content_type": "application/zip",
            "session_id": "conv_website_1",
        }
    ]
    assert stored_artifacts[0][0] == "file_site_zip_123"

    archive = tmp_path / "site.zip"
    archive.write_bytes(stored_artifacts[0][1])
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert names == result["entries"]
        assert "static/index.html" in names
        assert "static/assets/styles.css" in names
        assert "source/index.html" in names
        assert "source/README.md" in names
        assert "README.md" in names
        assert "website_manifest.json" in names
        manifest = json.loads(zf.read("website_manifest.json"))
        assert manifest["format"] == "bytedesk.website_package.v1"
        assert manifest["asset_file_ids"] == ["file_img_hero"]
        assert manifest["static_entries"] == [
            "static/assets/styles.css",
            "static/index.html",
        ]


def test_package_website_zip_requires_index_html(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "dist").mkdir(parents=True)

    result = json.loads(
        BytedeskPackageWebsiteZipTool().invoke(
            json.dumps({"static_dir": "dist"}),
            _ctx(workspace),
        )
    )

    assert result == {"ok": False, "error": "static_index_html_required"}


def test_package_website_zip_rejects_path_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(
        BytedeskPackageWebsiteZipTool().invoke(
            json.dumps({"static_dir": "../outside"}),
            _ctx(workspace),
        )
    )

    assert result["ok"] is False
    assert result["error"] == "path_escapes_workspace"


def test_package_website_zip_rejects_symlink_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_site(workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "dist" / "assets" / "secret.txt").symlink_to(outside)

    result = json.loads(
        BytedeskPackageWebsiteZipTool().invoke(
            json.dumps({"static_dir": "dist"}),
            _ctx(workspace),
        )
    )

    assert result == {
        "ok": False,
        "error": "symlink_not_supported",
        "path": "dist/assets/secret.txt",
    }


def test_package_website_zip_requires_workspace_and_session(tmp_path: Path) -> None:
    tool = BytedeskPackageWebsiteZipTool()

    no_workspace = json.loads(
        tool.invoke(
            json.dumps({"static_dir": "dist"}),
            ToolContext(task_id="task", agent_id="agent", workspace=None, conversation_id="conv"),
        )
    )
    assert no_workspace == {"ok": False, "error": "workspace_required"}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_site(workspace)
    no_session = json.loads(tool.invoke(json.dumps({"static_dir": "dist"}), _ctx(workspace, None)))
    assert no_session == {"ok": False, "error": "session_id_required"}


def test_package_tool_schema_and_extension_registration() -> None:
    assert BytedeskPackageWebsiteZipTool.name() == "bytedesk_package_website_zip"
    schema = BytedeskPackageWebsiteZipTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_package_website_zip"
    assert schema["function"]["parameters"]["required"] == ["static_dir"]

    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_package_website_zip" in factories
    tool = factories["bytedesk_package_website_zip"](object())
    assert tool.name() == "bytedesk_package_website_zip"
