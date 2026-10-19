"""文件名与文件类型工具函数。"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from .security import sanitize_filename

# 分类常量（资源分类界面与下载目录共用）
CATEGORY_DOCUMENT = "文档"
CATEGORY_SPREADSHEET = "表格"
CATEGORY_PRESENTATION = "演示文稿"
CATEGORY_ARCHIVE = "压缩包"
CATEGORY_IMAGE = "图片"
CATEGORY_VIDEO = "视频"
CATEGORY_AUDIO = "音频"
CATEGORY_PROGRAM = "程序与安装包"
CATEGORY_DATA = "数据与其他文件"
CATEGORY_UNKNOWN = "未知类型"

# 有序分类列表（界面分类标签顺序，不含“全部”）
CATEGORIES = [
    CATEGORY_DOCUMENT,
    CATEGORY_SPREADSHEET,
    CATEGORY_PRESENTATION,
    CATEGORY_ARCHIVE,
    CATEGORY_IMAGE,
    CATEGORY_VIDEO,
    CATEGORY_AUDIO,
    CATEGORY_PROGRAM,
    CATEGORY_DATA,
    CATEGORY_UNKNOWN,
]

# 常见可下载文件扩展名 -> 分类
EXTENSION_CATEGORY = {
    # 文档
    "pdf": CATEGORY_DOCUMENT, "doc": CATEGORY_DOCUMENT, "docx": CATEGORY_DOCUMENT,
    "txt": CATEGORY_DOCUMENT, "rtf": CATEGORY_DOCUMENT, "odt": CATEGORY_DOCUMENT,
    "md": CATEGORY_DOCUMENT, "epub": CATEGORY_DOCUMENT, "mobi": CATEGORY_DOCUMENT,
    # 表格
    "xls": CATEGORY_SPREADSHEET, "xlsx": CATEGORY_SPREADSHEET,
    "csv": CATEGORY_SPREADSHEET, "ods": CATEGORY_SPREADSHEET,
    # 演示文稿
    "ppt": CATEGORY_PRESENTATION, "pptx": CATEGORY_PRESENTATION,
    "odp": CATEGORY_PRESENTATION,
    # 压缩包
    "zip": CATEGORY_ARCHIVE, "rar": CATEGORY_ARCHIVE, "7z": CATEGORY_ARCHIVE,
    "tar": CATEGORY_ARCHIVE, "gz": CATEGORY_ARCHIVE, "bz2": CATEGORY_ARCHIVE,
    "xz": CATEGORY_ARCHIVE, "tgz": CATEGORY_ARCHIVE, "iso": CATEGORY_ARCHIVE,
    # 图片
    "jpg": CATEGORY_IMAGE, "jpeg": CATEGORY_IMAGE, "png": CATEGORY_IMAGE,
    "gif": CATEGORY_IMAGE, "webp": CATEGORY_IMAGE, "svg": CATEGORY_IMAGE,
    "bmp": CATEGORY_IMAGE, "tif": CATEGORY_IMAGE, "tiff": CATEGORY_IMAGE,
    "ico": CATEGORY_IMAGE, "avif": CATEGORY_IMAGE,
    # 视频
    "mp4": CATEGORY_VIDEO, "mkv": CATEGORY_VIDEO, "avi": CATEGORY_VIDEO,
    "mov": CATEGORY_VIDEO, "webm": CATEGORY_VIDEO, "flv": CATEGORY_VIDEO,
    "wmv": CATEGORY_VIDEO, "m4v": CATEGORY_VIDEO, "ts": CATEGORY_VIDEO,
    "mpg": CATEGORY_VIDEO, "mpeg": CATEGORY_VIDEO,
    # 音频
    "mp3": CATEGORY_AUDIO, "wav": CATEGORY_AUDIO, "flac": CATEGORY_AUDIO,
    "aac": CATEGORY_AUDIO, "ogg": CATEGORY_AUDIO, "m4a": CATEGORY_AUDIO,
    "opus": CATEGORY_AUDIO, "wma": CATEGORY_AUDIO,
    # 程序与安装包
    "exe": CATEGORY_PROGRAM, "msi": CATEGORY_PROGRAM, "msix": CATEGORY_PROGRAM,
    "apk": CATEGORY_PROGRAM, "dmg": CATEGORY_PROGRAM, "deb": CATEGORY_PROGRAM,
    "rpm": CATEGORY_PROGRAM,
    # 数据与其他文件
    "json": CATEGORY_DATA, "xml": CATEGORY_DATA, "sql": CATEGORY_DATA,
    "db": CATEGORY_DATA, "sqlite": CATEGORY_DATA, "yaml": CATEGORY_DATA,
    "yml": CATEGORY_DATA, "log": CATEGORY_DATA, "ini": CATEGORY_DATA,
    "conf": CATEGORY_DATA, "dat": CATEGORY_DATA,
}

DOWNLOAD_EXTENSIONS = set(EXTENSION_CATEGORY.keys())

# MIME 类型 -> 分类（集中管理，供 Content-Type 识别）
MIME_CATEGORY = {
    # 文档
    "application/pdf": CATEGORY_DOCUMENT,
    "application/msword": CATEGORY_DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": CATEGORY_DOCUMENT,
    "application/vnd.oasis.opendocument.text": CATEGORY_DOCUMENT,
    "application/rtf": CATEGORY_DOCUMENT,
    "application/epub+zip": CATEGORY_DOCUMENT,
    "text/plain": CATEGORY_DOCUMENT,
    # 表格
    "application/vnd.ms-excel": CATEGORY_SPREADSHEET,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": CATEGORY_SPREADSHEET,
    "application/vnd.oasis.opendocument.spreadsheet": CATEGORY_SPREADSHEET,
    "text/csv": CATEGORY_SPREADSHEET,
    # 演示文稿
    "application/vnd.ms-powerpoint": CATEGORY_PRESENTATION,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": CATEGORY_PRESENTATION,
    "application/vnd.oasis.opendocument.presentation": CATEGORY_PRESENTATION,
    # 压缩包
    "application/zip": CATEGORY_ARCHIVE,
    "application/x-rar-compressed": CATEGORY_ARCHIVE,
    "application/x-7z-compressed": CATEGORY_ARCHIVE,
    "application/x-tar": CATEGORY_ARCHIVE,
    "application/gzip": CATEGORY_ARCHIVE,
    "application/x-bzip2": CATEGORY_ARCHIVE,
    "application/x-xz": CATEGORY_ARCHIVE,
    # 图片
    "image/jpeg": CATEGORY_IMAGE, "image/png": CATEGORY_IMAGE, "image/gif": CATEGORY_IMAGE,
    "image/webp": CATEGORY_IMAGE, "image/svg+xml": CATEGORY_IMAGE, "image/bmp": CATEGORY_IMAGE,
    "image/tiff": CATEGORY_IMAGE, "image/x-icon": CATEGORY_IMAGE, "image/avif": CATEGORY_IMAGE,
    # 视频
    "video/mp4": CATEGORY_VIDEO, "video/x-matroska": CATEGORY_VIDEO,
    "video/x-msvideo": CATEGORY_VIDEO, "video/quicktime": CATEGORY_VIDEO,
    "video/webm": CATEGORY_VIDEO, "video/x-flv": CATEGORY_VIDEO,
    "video/x-ms-wmv": CATEGORY_VIDEO, "video/mpeg": CATEGORY_VIDEO,
    # 音频
    "audio/mpeg": CATEGORY_AUDIO, "audio/wav": CATEGORY_AUDIO,
    "audio/flac": CATEGORY_AUDIO, "audio/aac": CATEGORY_AUDIO,
    "audio/ogg": CATEGORY_AUDIO, "audio/mp4": CATEGORY_AUDIO,
    "audio/x-ms-wma": CATEGORY_AUDIO, "audio/opus": CATEGORY_AUDIO,
    # 程序与安装包
    "application/x-msdownload": CATEGORY_PROGRAM,
    "application/x-msi": CATEGORY_PROGRAM,
    "application/vnd.android.package-archive": CATEGORY_PROGRAM,
    "application/x-debian-package": CATEGORY_PROGRAM,
    "application/x-rpm": CATEGORY_PROGRAM,
    "application/x-apple-diskimage": CATEGORY_PROGRAM,
    # 数据与其他文件
    "application/json": CATEGORY_DATA,
    "application/xml": CATEGORY_DATA,
    "text/xml": CATEGORY_DATA,
    "application/sql": CATEGORY_DATA,
    "application/x-sqlite3": CATEGORY_DATA,
    "application/x-yaml": CATEGORY_DATA,
}

# MIME 类型 -> 扩展名（无扩展名资源命名用）
MIME_EXTENSION = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip", "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z", "application/x-tar": "tar",
    "application/gzip": "gz",
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/svg+xml": "svg", "image/bmp": "bmp",
    "image/tiff": "tiff",
    "video/mp4": "mp4", "video/x-matroska": "mkv", "video/webm": "webm",
    "video/quicktime": "mov", "video/x-msvideo": "avi",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/flac": "flac",
    "audio/aac": "aac", "audio/ogg": "ogg", "audio/mp4": "m4a",
    "application/x-msdownload": "exe", "application/x-msi": "msi",
    "application/vnd.android.package-archive": "apk",
    "application/json": "json", "application/xml": "xml",
    "text/plain": "txt", "text/csv": "csv",
}


def _mime_type(value: str) -> str:
    """提取 MIME 主类型（去参数、小写）。"""
    return (value or "").split(";")[0].strip().lower()


def guess_extension(name: str) -> str:
    """从文件名或 URL 路径提取小写扩展名（不含点）。"""
    path = name.split("?")[0].split("#")[0]
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    ext = base.rsplit(".", 1)[-1].lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)
    return ext


def guess_category(ext: str) -> str:
    """下载目录分类用：未知类型归入“其他”。"""
    return EXTENSION_CATEGORY.get(ext.lower(), "其他")


def categorize_ext(ext: str) -> str:
    """资源分类用：扩展名 -> 分类，未知返回“未知类型”。"""
    return EXTENSION_CATEGORY.get((ext or "").lower(), CATEGORY_UNKNOWN)


def categorize_mime(mime: str) -> str:
    """资源分类用：MIME 类型 -> 分类，未知返回“未知类型”。"""
    return MIME_CATEGORY.get(_mime_type(mime), CATEGORY_UNKNOWN)


def extension_from_mime(mime: str) -> str:
    """MIME 类型 -> 扩展名（无扩展名资源命名用），未知返回空串。"""
    return MIME_EXTENSION.get(_mime_type(mime), "")


def filename_from_url(url: str) -> str:
    """从 URL 推导文件名（含扩展名）；无可用名时返回空字符串。"""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if not base:
        return ""
    # 去掉查询参数里可能混入的痕迹
    base = base.split("?")[0]
    return base


def filename_from_content_disposition(value: str) -> Optional[str]:
    """从 Content-Disposition 头解析文件名，优先 filename*=UTF-8''..."""
    if not value:
        return None
    # 优先 RFC 5987 filename*
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", value, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        if name:
            return name
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return None


def build_filename(url: str, content_disposition: str = "") -> str:
    """综合 Content-Disposition 与 URL 推导最终文件名（已安全清洗）。"""
    cd_name = filename_from_content_disposition(content_disposition)
    url_name = filename_from_url(url)
    if cd_name:
        ext = guess_extension(cd_name)
        if ext:  # CD 文件名带扩展名时优先使用
            return sanitize_filename(cd_name)
    if url_name:
        return sanitize_filename(url_name)
    if cd_name:
        return sanitize_filename(cd_name)
    return "download"
