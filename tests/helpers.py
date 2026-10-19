"""测试辅助函数（供 conftest 与各测试模块共享）。"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import Config
from app.db import Database
from app.engine import DownloadManager


def data(n: int) -> bytes:
    """生成确定性的 n 字节测试数据。"""
    return (b"0123456789abcdef" * (n // 16 + 1))[:n]


def make_config(tmp: Path) -> Config:
    cfg = Config(tmp / "config.json")
    cfg.set("allow_private_network", True)
    cfg.set("download_dir", str(tmp / "downloads"))
    cfg.set("global_concurrency", 4)
    cfg.set("per_domain_concurrency", 4)
    cfg.set("max_retries", 2)
    cfg.set("timeout", 15.0)
    return cfg


@asynccontextmanager
async def running_manager(tmp: Path, **overrides):
    cfg = make_config(tmp)
    for k, v in overrides.items():
        cfg.set(k, v)
    db = Database(tmp / "data" / "tasks.db")
    await db.connect()
    mgr = DownloadManager(cfg, db, tmp)
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()
        await db.close()


async def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def wait_status(mgr: DownloadManager, tid: int, status: str, timeout: float = 20.0) -> None:
    ok = await wait_until(lambda: mgr.tasks[tid].status == status, timeout)
    assert ok, (
        f"任务 {tid} 未达到状态 {status}，当前 {mgr.tasks[tid].status}，"
        f"错误：{mgr.tasks[tid].error}"
    )


async def wait_partial(mgr: DownloadManager, tid: int, timeout: float = 20.0) -> None:
    ok = await wait_until(
        lambda: 0 < mgr.tasks[tid].downloaded_bytes < mgr.tasks[tid].total_bytes,
        timeout,
    )
    assert ok, (
        f"任务 {tid} 未进入部分下载状态：status={mgr.tasks[tid].status} "
        f"downloaded={mgr.tasks[tid].downloaded_bytes}/{mgr.tasks[tid].total_bytes}"
    )
