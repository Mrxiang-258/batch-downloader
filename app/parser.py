"""网页解析：提取网页中的可下载链接。"""
from __future__ import annotations

import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from .fileutils import DOWNLOAD_EXTENSIONS, guess_extension
from .security import SecurityError, validate_url


def _looks_downloadable(url: str, has_download_attr: bool = False) -> bool:
    """判断链接是否像文件下载链接（依据扩展名或 download 属性）。"""
    if has_download_attr:
        return True
    path = urllib.parse.urlparse(url).path
    return guess_extension(path) in DOWNLOAD_EXTENSIONS


def extract_links(html: str, base_url: str) -> list[dict[str, Any]]:
    """从 HTML 中提取所有链接，标注是否为可下载文件。

    返回：[{"url", "text", "downloadable"}]
    注意：base_url 仅用于拼接相对链接，调用方应已完成 SSRF 校验。
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict[str, Any]] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urllib.parse.urljoin(base_url, href)
        # 只保留绝对 http/https 地址
        if not full.startswith(("http://", "https://")):
            continue
        if full in seen:
            continue
        seen.add(full)
        text = (a.get_text(strip=True) or "").strip() or a.get("title", "").strip() or full
        links.append({
            "url": full,
            "text": text[:200],
            "downloadable": _looks_downloadable(full, a.has_attr("download")),
        })

    # 补充 <source src>（视频/音频）与 <img src> 中的媒体文件
    for tag, attr in (("source", "src"), ("img", "src"), ("video", "src"), ("audio", "src")):
        for el in soup.find_all(tag, attrs={attr: True}):
            src = (el.get(attr) or "").strip()
            if not src or src.startswith("data:"):
                continue
            full = urllib.parse.urljoin(base_url, src)
            if not full.startswith(("http://", "https://")) or full in seen:
                continue
            seen.add(full)
            links.append({
                "url": full,
                "text": (el.get("alt") or el.get("title") or full).strip()[:200],
                "downloadable": True,
            })

    return links


def extract_urls_from_text(text: str) -> list[str]:
    """从纯文本（TXT/CSV/粘贴）中逐行提取 URL，忽略注释与空行。"""
    urls: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # CSV：取逗号/制表符/分号分隔后的第一个字段
        for sep in (",", "\t", ";"):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break
        if line.startswith(("http://", "https://")):
            urls.append(line)
    return urls
