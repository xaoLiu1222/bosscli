"""Image preparation and signed upload envelopes (App 14.180 capture, 2026-11-11).

This module performs no network I/O. JPEG encoding is a local policy, not a claim of
byte-identical Android compression. Authentication material must come from the caller's session.
"""
from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import signer, yzwg

UPLOAD_HOST = "https://api-and.zhipin.com"
PRECHECK_PATH = "/api/zpupload/quicklyUpload"
UPLOAD_IMAGE_PATH = "/api/zpupload/uploadSingle"
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # local safety policy; NOT a measured server limit
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class UploadError(RuntimeError):
    """A rejected or malformed upload response; no credentials are included in the message."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes = field(repr=False)
    filename: str
    width: int
    height: int
    format: str
    original: bool

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()

    def summary(self) -> dict:
        return {"filename": self.filename, "width": self.width, "height": self.height,
                "format": self.format, "bytes": len(self.data), "md5": self.md5,
                "original": self.original}


def prepare_image(path: str | Path, *, original: bool = False) -> PreparedImage:
    """Read once, validate a single-frame JPEG/PNG/WebP, and optionally normalize to JPEG.

    Default: honor EXIF orientation, composite transparency on white, encode RGB JPEG at
    quality 90, and omit EXIF/GPS metadata. --original preserves bytes (including metadata).
    File/pixel caps bound local resource use; animated images and other formats are rejected.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"图片文件不存在: {source}")
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片超过本地 20 MiB 安全上限（不是服务端限制）")
    with source.open("rb") as f:
        raw = f.read(MAX_IMAGE_BYTES + 1)
    if not raw:
        raise ValueError("图片文件为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过本地 20 MiB 安全上限（不是服务端限制）")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt = image.format
                if fmt not in ALLOWED_FORMATS:
                    raise ValueError("仅支持 JPEG、PNG、WebP 图片")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片超过本地 4000 万像素安全上限")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("暂不支持动画或多帧图片，请先另存为单张图片")
                image.load()  # reject truncated/corrupt images before any network request
                if original:
                    return PreparedImage(raw, source.name, image.width, image.height, fmt, True)
                upright = ImageOps.exif_transpose(image)
                if upright.mode == "I" or upright.mode.startswith("I;16"):
                    # Pillow's direct I;16 -> RGB conversion clips everything above 255.
                    # Normalize the full unsigned 16-bit range instead of washing it white.
                    low, high = upright.getextrema()
                    if low < 0 or high > 65535:
                        raise ValueError("暂不支持该整数图片色深，请转换为 8 位图片或使用 --original")
                    upright = upright.convert("I").point(lambda value: value * (255.0 / 65535)).convert("L")
                rgba = upright.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                rgb = Image.alpha_composite(background, rgba).convert("RGB")
                # New image + no exif argument: do not propagate EXIF/GPS to the output.
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=90)
                data = output.getvalue()
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("转换后的图片超过本地 20 MiB 安全上限")
                return PreparedImage(data, source.stem + ".jpg", rgb.width, rgb.height, "JPEG", False)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("无法读取图片，文件已损坏、格式不受支持或尺寸过大") from exc


def required_text(value, field_name: str) -> str:
    """Fail closed on missing credentials and template placeholders, without echoing their values."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"上传需要 {field_name}，请使用你自己账号的有效会话信息")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field_name} 包含非法控制字符，请重新复制有效值")
    value = value.strip()
    if (value.startswith("<") and value.endswith(">")) or value.startswith("{{"):
        raise ValueError(f"{field_name} 仍是模板占位符，请先填写有效值")
    return value


def upload_envelope(params: dict, api_path: str, key: str, app_id: str = "1003") -> dict[str, str]:
    """Only sp/sig/app_id go on the wire. No raw params/query or body checksum strA.

    App m.e signs the first 5000 characters of strD, but encrypts its complete contents.
    app_id is added AFTER signing, never inside strD.
    """
    signed = signer.sign_request(params, api_path, key)
    signature = yzwg.native_signature((api_path + signed["strD"][:5000]).encode("utf-8"), key)
    return {"sp": signed["sp"], "sig": signature, "app_id": str(app_id)}


def check_upload_response(response, stage: str) -> dict:
    """Do not turn code=None, invalid zpData, or a partial cache hit into a successful upload."""
    if not isinstance(response, dict) or response.get("code") != 0 or isinstance(response.get("code"), bool):
        code = response.get("code") if isinstance(response, dict) else None
        safe_code = str(code) if isinstance(code, (int, float)) else "unknown"
        raise UploadError(f"{stage}失败（code={safe_code}），已停止，不会继续上传")
    data = response.get("zpData")
    if not isinstance(data, dict):
        raise UploadError(f"{stage}返回了无效 zpData，已停止")
    return data


def has_image_urls(data: dict) -> bool:
    """Both urls are needed to construct the eventual image message; that message is out of scope."""
    return all(isinstance(data.get(k), str) and bool(data[k].strip()) for k in ("url", "tinyUrl"))
