"""配置管理：加载/保存 config.json。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    # 默认下载目录；空字符串表示使用 <项目>/downloads
    "download_dir": "",
    # 全局并发数
    "global_concurrency": 4,
    # 单域名并发数
    "per_domain_concurrency": 2,
    # 连接/读取超时（秒）
    "timeout": 30.0,
    # 失败最大重试次数
    "max_retries": 3,
    # 下载分块大小（字节）
    "chunk_size": 64 * 1024,
    # 高速下载模式（关闭时使用默认并发策略）
    "high_speed": False,
    # 高速模式下的全局并发数（上限 32）
    "high_speed_global_concurrency": 8,
    # 高速模式下的单域名并发数（上限 8）
    "high_speed_per_domain_concurrency": 4,
    # 启用多连接分块下载的最小文件大小（字节），默认 10MB，上限 1GB
    "chunk_threshold": 10 * 1024 * 1024,
    # 单文件最大分块连接数（上限 8）
    "max_chunks_per_file": 4,
    # 是否对同一 URL 去重
    "dedupe": True,
    # 重名文件策略：rename / overwrite / skip
    "conflict_policy": "rename",
    # 分类保存：none / type / domain / batch
    "classify_by": "none",
    # 是否允许访问内网/本机地址（默认关闭；仅用于下载用户明确授权的局域网资源，如 NAS）
    "allow_private_network": False,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    # 服务监听地址（仅本机）
    "host": "127.0.0.1",
    "port": 8000,
}


class Config:
    """简单的 JSON 配置封装，损坏时回退默认值。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update(raw)
            except (OSError, json.JSONDecodeError):
                # 配置损坏时回退默认值，不中断启动
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, mapping: dict) -> None:
        for k, v in mapping.items():
            if k in DEFAULT_CONFIG:
                self.data[k] = v

    def resolved_download_dir(self, project_root: Path) -> Path:
        d = str(self.get("download_dir", "")).strip()
        if not d:
            d = str(project_root / "downloads")
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def public_view(self, project_root: Path) -> dict:
        """返回给前端的配置视图（不含敏感信息，本配置本来也无密钥）。"""
        view = dict(self.data)
        view["resolved_download_dir"] = str(self.resolved_download_dir(project_root))
        return view
