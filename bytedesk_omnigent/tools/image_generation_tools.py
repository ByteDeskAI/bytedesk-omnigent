"""ByteDesk image generation tool for Omnigent agents."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openai.com/v1"
_MODEL = "gpt-image-2"
_TIMEOUT_S = 150.0

_SECRET_API_KEY = ("OPENAI_API_KEY", "BYTEDESK_OPENAI_API_KEY", "OMNIGENT_OPENAI_API_KEY")
_SECRET_BASE_URL = ("OPENAI_BASE_URL", "BYTEDESK_OPENAI_BASE_URL")

_DEFAULT_SIZE = "1024x1024"
_MIN_PIXELS = 655_360
_MAX_PIXELS = 8_294_400
_MAX_EDGE = 3_840
_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")
_ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
_ALLOWED_FORMATS = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_ALLOWED_BACKGROUNDS = {"auto", "opaque"}
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _first_secret_or_env(names: tuple[str, ...]) -> str:
    """Return the first non-empty configured value from secrets, then env."""
    from omnigent.onboarding.secrets import load_secret

    for name in names:
        value = (load_secret(name) or "").strip()
        if value:
            return value
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


class ImageGenerationNotConfiguredError(RuntimeError):
    """Raised when no OpenAI API key is configured."""


class ImageGenerationResponseError(RuntimeError):
    """Raised when the image API response does not contain image bytes."""


@dataclass(frozen=True)
class GeneratedImage:
    """Generated image payload returned by the provider adapter."""

    data: bytes
    model: str
    output_format: str
    revised_prompt: str | None = None
    usage: dict[str, Any] | None = None


class _OpenAIImageGenerationClient:
    """Adapter over OpenAI's Image API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        model: str = _MODEL,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/") or None
        self._api_key = api_key.strip() if api_key is not None else None
        self._client = client
        self._model = model
        self._resolved = api_key is not None

    def _resolve_credentials(self) -> None:
        if self._resolved:
            self._base_url = self._base_url or _BASE_URL
            return
        self._api_key = _first_secret_or_env(_SECRET_API_KEY)
        self._base_url = _first_secret_or_env(_SECRET_BASE_URL).rstrip("/") or _BASE_URL
        self._resolved = True

    def _require_configured(self) -> None:
        self._resolve_credentials()
        if not self._api_key:
            raise ImageGenerationNotConfiguredError(_SECRET_API_KEY[0])

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self._base_url or _BASE_URL, timeout=_TIMEOUT_S)
        return self._client

    def generate(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        background: str,
    ) -> GeneratedImage:
        """Generate one image and return decoded bytes."""
        self._require_configured()
        response = self._http().post(
            "/images/generations",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "prompt": prompt,
                "size": size,
                "quality": quality,
                "output_format": output_format,
                "background": background,
            },
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        first = data[0] if isinstance(data, list) and data else {}
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ImageGenerationResponseError("missing b64_json")
        try:
            raw = base64.b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenerationResponseError("invalid b64_json") from exc
        return GeneratedImage(
            data=raw,
            model=self._model,
            output_format=output_format,
            revised_prompt=first.get("revised_prompt") if isinstance(first, dict) else None,
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        )


class BytedeskGenerateImageTool(Tool):
    """Generate an image and persist it as an Omnigent session file."""

    def __init__(self, client: _OpenAIImageGenerationClient | None = None) -> None:
        self._client = client or _OpenAIImageGenerationClient()

    @classmethod
    def name(cls) -> str:
        return "bytedesk_generate_image"

    @classmethod
    def description(cls) -> str:
        return (
            "Generate one image from a text prompt using the configured OpenAI image "
            "provider, store it as an Omnigent session file, and return a file_id."
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
                        "prompt": {
                            "type": "string",
                            "description": "Detailed prompt describing the image to generate.",
                        },
                        "filename": {
                            "type": "string",
                            "description": (
                                "Optional output filename. The extension is normalized to "
                                "match output_format."
                            ),
                        },
                        "size": {
                            "type": "string",
                            "default": _DEFAULT_SIZE,
                            "description": (
                                "Output dimensions such as 1024x1024, 1536x1024, "
                                "2048x1152, 3840x2160, or auto. Numeric sizes must be "
                                "multiples of 16, at most 3840 px on either edge, within "
                                "655360..8294400 total pixels, and no more than 3:1."
                            ),
                        },
                        "quality": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_QUALITIES),
                            "default": "medium",
                            "description": "Generation quality. Low is cheaper/faster; high costs more.",
                        },
                        "output_format": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_FORMATS),
                            "default": "png",
                            "description": "Image file format.",
                        },
                        "background": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_BACKGROUNDS),
                            "default": "opaque",
                            "description": "Background mode. Transparent is not available for gpt-image-2.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments_json"})

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return json.dumps({"ok": False, "error": "missing required 'prompt'"})
        if ctx.conversation_id is None:
            return json.dumps({"ok": False, "error": "image_generation_requires_session"})

        size = _validated_size(args.get("size"))
        quality = _validated_choice(args.get("quality"), _ALLOWED_QUALITIES, "medium")
        output_format = _validated_choice(args.get("output_format"), set(_ALLOWED_FORMATS), "png")
        background = _validated_choice(args.get("background"), _ALLOWED_BACKGROUNDS, "opaque")
        if None in (size, quality, output_format, background):
            return json.dumps({"ok": False, "error": "invalid_image_generation_options"})

        try:
            image = self._client.generate(
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
            )
            return _store_generated_image(
                image=image,
                filename=_filename(args.get("filename"), output_format),
                content_type=_ALLOWED_FORMATS[output_format],
                session_id=ctx.conversation_id,
                prompt=prompt,
                size=size,
                quality=quality,
                background=background,
            )
        except ImageGenerationNotConfiguredError:
            return json.dumps({"ok": False, "error": "openai_image_generation_not_configured"})
        except ImageGenerationResponseError:
            return json.dumps({"ok": False, "error": "openai_image_generation_bad_response"})
        except httpx.HTTPStatusError as exc:
            logger.warning("openai image generation returned HTTP %s", exc.response.status_code)
            return json.dumps(
                {
                    "ok": False,
                    "error": "openai_image_generation_http_error",
                    "status": exc.response.status_code,
                }
            )
        except httpx.HTTPError as exc:
            logger.warning("openai image generation request failed: %s", type(exc).__name__)
            return json.dumps({"ok": False, "error": "openai_image_generation_request_failed"})


def _validated_choice(value: Any, allowed: set[str], default: str) -> str | None:
    if value is None or value == "":
        return default
    candidate = str(value).strip().lower()
    return candidate if candidate in allowed else None


def _validated_size(value: Any) -> str | None:
    if value is None or value == "":
        return _DEFAULT_SIZE
    candidate = str(value).strip().lower()
    if candidate == "auto":
        return candidate
    match = _SIZE_RE.fullmatch(candidate)
    if match is None:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    if width % 16 != 0 or height % 16 != 0:
        return None
    if width > _MAX_EDGE or height > _MAX_EDGE:
        return None
    pixels = width * height
    if pixels < _MIN_PIXELS or pixels > _MAX_PIXELS:
        return None
    if max(width, height) / min(width, height) > 3:
        return None
    return f"{width}x{height}"


def _filename(value: Any, output_format: str) -> str:
    base = str(value or "").strip()
    if not base:
        base = f"generated-image-{int(time.time())}"
    safe = _FILENAME_RE.sub("-", base).strip(".-") or "generated-image"
    extension = "jpg" if output_format == "jpeg" else output_format
    root = safe.rsplit(".", 1)[0] if "." in safe else safe
    return f"{root}.{extension}"


def _store_generated_image(
    *,
    image: GeneratedImage,
    filename: str,
    content_type: str,
    session_id: str,
    prompt: str,
    size: str,
    quality: str,
    background: str,
) -> str:
    from omnigent.runtime import get_artifact_store, get_file_store

    file_store = get_file_store()
    artifact_store = get_artifact_store()
    if file_store is None or artifact_store is None:
        return json.dumps({"ok": False, "error": "image_file_store_not_available"})

    file_record = file_store.create(
        filename=filename,
        bytes=len(image.data),
        content_type=content_type,
        session_id=session_id,
    )
    artifact_store.put(file_record.id, image.data)
    result: dict[str, Any] = {
        "ok": True,
        "file_id": file_record.id,
        "filename": filename,
        "bytes": len(image.data),
        "content_type": content_type,
        "model": image.model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "background": background,
        "output_format": image.output_format,
    }
    if image.revised_prompt:
        result["revised_prompt"] = image.revised_prompt
    if image.usage:
        result["usage"] = image.usage
    return json.dumps(result)
