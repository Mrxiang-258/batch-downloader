"""安全校验：URL/SSRF 防护、文件名清洗、路径校验。"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
import urllib.parse
from pathlib import Path


class SecurityError(Exception):
    """安全相关的错误，附带面向用户的说明。"""


# 需要拦截的私有/保留网段
_PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# 需要拦截的主机名（或后缀）
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "169.254.169.254",
}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".localhost.localdomain")

# Windows 保留设备名
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return True  # 无法解析则按危险处理
    return any(ip in net for net in _PRIVATE_NETS)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_is_blocked(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True
    if any(host.endswith(s) for s in _BLOCKED_SUFFIXES):
        return True
    if _is_ip_literal(host):
        return _is_private_ip(host)
    return False


def validate_url(url: str, allow_private: bool = False) -> str:
    """校验 URL 合法性并返回规范化后的字符串。

    仅允许 http/https 公开地址；拦截 localhost、内网 IP、云元数据地址等。
    该方法为同步实现，用作 httpx 请求钩子，重定向后的每个跳转都会重新校验。
    allow_private=True 时放行内网/本机地址（用于用户明确授权的局域网资源）。
    """
    url = (url or "").strip()
    if not url:
        raise SecurityError("URL 为空")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError(f"不支持的协议：{parsed.scheme or '(空)'}，仅支持 http/https")

    host = parsed.hostname
    if not host:
        raise SecurityError("无法从 URL 中解析出主机名")

    if not allow_private and _host_is_blocked(host):
        raise SecurityError(
            f"已拦截对非公开地址的访问：{host}（禁止访问 localhost / 内网 / 元数据地址）"
        )

    # 检查 URL 中的用户信息（避免在 URL 里泄露凭据，也属可疑输入）
    if parsed.username or parsed.password:
        raise SecurityError("URL 中不允许携带用户名/密码")

    return url


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """将任意字符串清洗为安全的文件名（去掉路径、非法字符、保留名等）。"""
    if name is None:
        name = ""
    name = name.strip()
    if not name:
        return fallback

    # 统一换行/制表符为空格，去掉路径分隔符
    name = name.replace("\\", "_").replace("/", "_")
    # 去掉非法字符与控制字符
    name = _INVALID_CHARS.sub("_", name)
    # 去掉文件名中的盘符标记
    name = re.sub(r"^[A-Za-z]:", "_", name)
    # 压缩连续空格
    name = re.sub(r"\s+", " ", name).strip()
    # Windows 不允许以点/空格结尾
    name = name.strip(" .")
    # 清除路径穿越残留的 ".." 序列
    name = name.replace("..", "_")
    # 去掉开头的点（避免隐藏文件、以及 ".." 残留）
    name = name.lstrip(".")

    if not name:
        return fallback

    # Windows 保留设备名（CON、NUL 等），加下划线前缀规避
    base = name.split(".")[0].upper()
    if base in _WINDOWS_RESERVED:
        name = "_" + name

    # 控制长度，避免超出文件系统限制
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        keep = 180 - len(ext) - (1 if dot else 0)
        name = (stem[:keep] + (dot + ext if dot else "")) if keep > 1 else name[:180]

    return name or fallback


def safe_join(base_dir: str | Path, filename: str) -> Path:
    """将文件名安全地拼接到下载目录内，确保结果不逃逸出该目录。"""
    base = Path(base_dir).resolve()
    candidate = (base / filename).resolve()
    if candidate != base and base not in candidate.parents:
        raise SecurityError(f"非法路径：{filename!r} 逃逸出下载目录")
    return candidate


def path_is_under(base_dir: str | Path, target: str | Path) -> bool:
    """判断 target 是否位于 base_dir 之内（用于打开文件/目录前的校验）。"""
    try:
        base = Path(base_dir).resolve()
        t = Path(target).resolve()
        return t == base or base in t.parents
    except OSError:
        return False
