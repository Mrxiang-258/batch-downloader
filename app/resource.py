"""网页资源数据模型与分类逻辑。

将网页中嗅探到的可下载资源统一为 Resource，综合 URL 扩展名、
HTTP Content-Type、Content-Disposition 识别类型并归类。
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from .fileutils import (
    CATEGORY_UNKNOWN,
    categorize_ext,
    categorize_mime,
    extension_from_mime,
    filename_from_url,
    guess_extension,
)
from .security import sanitize_filename

# 资源状态
STATUS_AVAILABLE = "available"        # 待下载
STATUS_AUTH = "auth_required"         # 可能需要授权
STATUS_INACCESSIBLE = "inaccessible"  # 不可访问
STATUS_UNKNOWN = "unknown_type"       # 类型未知

STATUS_LABELS = {
    STATUS_AVAILABLE: "待下载",
    STATUS_AUTH: "可能需要授权",
    STATUS_INACCESSIBLE: "不可访问",
    STATUS_UNKNOWN: "类型未知",
}


@dataclass
class Resource:
    url: str
    filename: str = ""
    filetype: str = ""
    category: str = CATEGORY_UNKNOWN
    size: int = 0                 # 0 表示未知
    mime: str = ""
    source_url: str = ""
    source_domain: str = ""
    status: str = STATUS_AVAILABLE
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "filename": self.filename,
            "filetype": self.filetype,
            "category": self.category,
            "size": self.size,
            "mime": self.mime,
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "note": self.note,
        }


def domain_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def classify(url: str, mime: str = "", cd_filename: str = "") -> tuple[str, str, str, str]:
    """综合 URL 扩展名、Content-Type、Content-Disposition 确定资源类型。

    返回 (category, filetype, filename, note)。
    note 非空表示有异常说明（例如“无法识别类型”）。
    """
    mime_type = (mime or "").split(";")[0].strip().lower()

    # 1. Content-Disposition 文件名最可靠
    if cd_filename:
        cd_ext = guess_extension(cd_filename)
        if cd_ext:
            return categorize_ext(cd_ext), cd_ext, sanitize_filename(cd_filename), ""
        cat = categorize_mime(mime)
        ext = extension_from_mime(mime)
        name = sanitize_filename(cd_filename)
        if cat != CATEGORY_UNKNOWN and ext:
            return cat, ext, name + "." + ext, ""
        if cat != CATEGORY_UNKNOWN:
            return cat, "", name, ""
        return CATEGORY_UNKNOWN, "", name, "无法识别类型"

    # 2. URL 扩展名（对 text/html 响应交叉验证，避免把网页当文件）
    url_ext = guess_extension(url)
    if url_ext and url_ext not in ("html", "htm", "php", "asp", "aspx", "jsp"):
        if mime_type == "text/html":
            # 扩展名像文件但服务器返回网页，视为不可靠
            return CATEGORY_UNKNOWN, url_ext, filename_from_url(url) or "download", "响应为网页"
        return categorize_ext(url_ext), url_ext, filename_from_url(url) or "download", ""

    # 3. 仅凭 MIME 类型
    cat = categorize_mime(mime)
    ext = extension_from_mime(mime)
    name = filename_from_url(url)
    if cat != CATEGORY_UNKNOWN:
        if not name or name in ("download", ""):
            name = "file." + ext if ext else "download"
        elif ext and not name.lower().endswith("." + ext):
            name = name + "." + ext
        return cat, ext, name, ""

    # 4. 无法识别
    return CATEGORY_UNKNOWN, "", filename_from_url(url) or "download", "无法识别类型"


def normalize_url(url: str) -> str:
    """规范化 URL 用于去重，保留查询参数中的签名/令牌。"""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url
    # 小写 scheme 与 host，保留 path 与 query 原样（不重排参数）
    netloc = parsed.netloc.lower()
    # 去掉默认端口，减少重复
    if parsed.port in (80, 443):
        netloc = parsed.hostname or netloc
    return urllib.parse.urlunparse((
        parsed.scheme.lower(), netloc, parsed.path or "/",
        parsed.params, parsed.query, "",
    ))
