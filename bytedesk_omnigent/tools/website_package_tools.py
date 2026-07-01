"""ByteDesk website packaging tools for Omnigent agents."""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.upload_file import safe_resolve

_CONTENT_TYPE = "application/zip"
_DEFAULT_FILENAME = "website-package.zip"
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_ENTRY_COUNT = 2_000
_MAX_PACKAGE_BYTES = 50 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class BytedeskPackageWebsiteZipTool(Tool):
    """Package generated website files as a downloadable session artifact."""

    @classmethod
    def name(cls) -> str:
        return "bytedesk_package_website_zip"

    @classmethod
    def description(cls) -> str:
        return (
            "Package a generated website from the workspace into a downloadable "
            "zip session file. Requires a static directory with index.html and "
            "optionally includes editable source files."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "static_dir": {
                            "type": "string",
                            "description": (
                                "Relative workspace directory containing the deployable "
                                "static site. Must include index.html."
                            ),
                        },
                        "source_dir": {
                            "type": "string",
                            "description": (
                                "Optional relative workspace directory containing editable "
                                "source files. If omitted, static_dir is reused."
                            ),
                        },
                        "filename": {
                            "type": "string",
                            "description": "Optional zip filename. The .zip extension is added.",
                        },
                        "asset_file_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional generated image/file IDs used while building the site. "
                                "Stored in website_manifest.json for traceability."
                            ),
                        },
                    },
                    "required": ["static_dir"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return _error("invalid_arguments_json")

        if ctx.workspace is None:
            return _error("workspace_required")
        if ctx.conversation_id is None:
            return _error("session_id_required")

        static_rel = str(args.get("static_dir") or "").strip()
        if not static_rel:
            return _error("static_dir_required")
        source_rel = str(args.get("source_dir") or "").strip() or static_rel

        try:
            static_dir = _resolve_dir(static_rel, ctx.workspace)
            source_dir = _resolve_dir(source_rel, ctx.workspace)
        except ValueError as exc:
            return _error(str(exc))

        if not (static_dir / "index.html").is_file():
            return _error("static_index_html_required")

        asset_file_ids = [
            str(item).strip()
            for item in args.get("asset_file_ids", [])
            if str(item).strip()
        ]

        try:
            package = _build_zip(
                workspace=ctx.workspace,
                static_dir=static_dir,
                source_dir=source_dir,
                asset_file_ids=asset_file_ids,
            )
        except _PackageError as exc:
            return json.dumps(exc.payload)

        if len(package.data) > _MAX_PACKAGE_BYTES:
            return _error("package_too_large")

        return _store_zip(
            package=package,
            filename=_filename(args.get("filename")),
            session_id=ctx.conversation_id,
        )


class _PackageError(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload))


class _Package:
    def __init__(self, data: bytes, entries: list[str]) -> None:
        self.data = data
        self.entries = entries


def _error(error: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


def _resolve_dir(value: str, workspace: Path) -> Path:
    try:
        resolved = safe_resolve(value, workspace)
    except ValueError as exc:
        message = str(exc)
        if "escapes workspace" in message or "escapes" in message:
            raise ValueError("path_escapes_workspace") from exc
        raise
    if not resolved.is_dir():
        raise ValueError("directory_not_found")
    return resolved


def _filename(value: Any) -> str:
    base = str(value or "").strip() or _DEFAULT_FILENAME
    safe = _FILENAME_RE.sub("-", base).strip(".-") or "website-package"
    root = safe[:-4] if safe.lower().endswith(".zip") else safe
    return f"{root}.zip"


def _iter_files(base: Path, prefix: str, workspace: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        rel_workspace = path.relative_to(workspace).as_posix()
        if path.is_symlink():
            raise _PackageError(
                {
                    "ok": False,
                    "error": "symlink_not_supported",
                    "path": rel_workspace,
                }
            )
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(workspace.resolve()):
            raise _PackageError(
                {
                    "ok": False,
                    "error": "path_escapes_workspace",
                    "path": rel_workspace,
                }
            )
        rel = path.relative_to(base).as_posix()
        files.append((posixpath.join(prefix, rel), path))
    return files


def _build_zip(
    *,
    workspace: Path,
    static_dir: Path,
    source_dir: Path,
    asset_file_ids: list[str],
) -> _Package:
    static_files = _iter_files(static_dir, "static", workspace)
    source_files = _iter_files(source_dir, "source", workspace)
    if len(static_files) + len(source_files) + 2 > _MAX_ENTRY_COUNT:
        raise _PackageError({"ok": False, "error": "too_many_package_entries"})

    static_entries = [name for name, _path in static_files]
    source_entries = [name for name, _path in source_files]
    manifest = {
        "format": "bytedesk.website_package.v1",
        "asset_file_ids": asset_file_ids,
        "static_entries": static_entries,
        "source_entries": source_entries,
    }
    readme = (
        "# Website Package\n\n"
        "This archive was generated by a ByteDesk Omnigent website workflow.\n\n"
        "- `static/` contains the deployable HTML/CSS/JS site.\n"
        "- `source/` contains editable source files for follow-up changes.\n"
        "- `website_manifest.json` lists generated asset references and archive entries.\n"
    )

    entries: list[str] = []
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name, path in [*static_files, *source_files]:
            _write_zip_bytes(archive, archive_name, path.read_bytes())
            entries.append(archive_name)
        _write_zip_bytes(archive, "README.md", readme.encode("utf-8"))
        entries.append("README.md")
        _write_zip_bytes(
            archive,
            "website_manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        entries.append("website_manifest.json")
    return _Package(out.getvalue(), entries)


def _write_zip_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def _store_zip(
    *,
    package: _Package,
    filename: str,
    session_id: str,
) -> str:
    from omnigent.runtime import get_artifact_store, get_file_store

    file_store = get_file_store()
    artifact_store = get_artifact_store()
    if file_store is None or artifact_store is None:
        return _error("file_store_not_available")

    file_record = file_store.create(
        filename=filename,
        bytes=len(package.data),
        content_type=_CONTENT_TYPE,
        session_id=session_id,
    )
    artifact_store.put(file_record.id, package.data)
    return json.dumps(
        {
            "ok": True,
            "file_id": file_record.id,
            "session_id": session_id,
            "filename": filename,
            "bytes": len(package.data),
            "content_type": _CONTENT_TYPE,
            "download_url": f"/v1/sessions/{session_id}/resources/files/{file_record.id}/content",
            "entries": package.entries,
        }
    )
