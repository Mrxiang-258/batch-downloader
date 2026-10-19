"""任务数据模型与状态定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class Status:
    PENDING = "pending"          # 等待中
    DOWNLOADING = "downloading"  # 下载中
    PAUSED = "paused"            # 已暂停
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"            # 失败
    CANCELED = "canceled"        # 已取消
    SKIPPED = "skipped"          # 已跳过（重名且策略为跳过）


STATUS_LABELS = {
    Status.PENDING: "等待中",
    Status.DOWNLOADING: "下载中",
    Status.PAUSED: "已暂停",
    Status.COMPLETED: "已完成",
    Status.FAILED: "失败",
    Status.CANCELED: "已取消",
    Status.SKIPPED: "已跳过",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 需要持久化到数据库的字段（不含运行时字段 speed / eta / multi_chunk）
PERSIST_FIELDS = [
    "url", "status", "filename", "filetype", "save_dir", "final_path",
    "temp_path", "total_bytes", "downloaded_bytes", "file_size", "error",
    "retry_count", "max_retries", "source", "batch_id", "etag",
    "last_modified", "resume_supported", "chunk_state", "created_at",
    "updated_at", "started_at", "completed_at",
]


@dataclass
class Task:
    id: int = 0
    url: str = ""
    status: str = Status.PENDING
    filename: str = ""
    filetype: str = ""
    save_dir: str = ""
    final_path: str = ""
    temp_path: str = ""
    total_bytes: int = 0
    downloaded_bytes: int = 0
    file_size: int = 0
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3
    source: str = "direct"       # direct / webpage / file
    batch_id: str = ""
    etag: str = ""
    last_modified: str = ""
    resume_supported: bool = False
    chunk_state: str = ""        # 分块下载进度（JSON，持久化）
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    # 运行时字段（不持久化）
    speed: float = field(default=0.0, repr=False)
    eta: float = field(default=0.0, repr=False)
    multi_chunk: bool = field(default=False, repr=False)  # 是否正用多连接分块下载

    def to_dict(self) -> dict[str, Any]:
        d = {f: getattr(self, f) for f in PERSIST_FIELDS}
        d["id"] = self.id
        d["speed"] = round(self.speed, 1)
        d["eta"] = round(self.eta, 1)
        d["multi_chunk"] = self.multi_chunk
        d["status_label"] = STATUS_LABELS.get(self.status, self.status)
        d["progress"] = round(
            (self.downloaded_bytes / self.total_bytes * 100), 1
        ) if self.total_bytes > 0 else 0.0
        return d
