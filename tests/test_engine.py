"""下载引擎测试：单文件、批量、重名、续传、重试、取消、失败等。"""
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.db import Database
from app.engine import DownloadManager
from app.models import Status
from app.security import SecurityError

from .helpers import data, running_manager, wait_partial, wait_status, wait_until


async def _add(mgr: DownloadManager, url: str, **kw):
    r = await mgr.add_urls([url], **kw)
    assert r["added"] == 1
    tid = max(mgr.tasks)
    return tid


def _final_bytes(mgr: DownloadManager, tid: int) -> bytes:
    return Path(mgr.tasks[tid].final_path).read_bytes()


def test_single_download(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/file/12345")
            await wait_status(mgr, tid, Status.COMPLETED)
            t = mgr.tasks[tid]
            assert t.total_bytes == 12345
            assert t.downloaded_bytes == 12345
            assert _final_bytes(mgr, tid) == data(12345)
    asyncio.run(main())


def test_batch_download(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            urls = [f"{server.url}/file/{n}" for n in (1000, 2000, 3000, 4000)]
            r = await mgr.add_urls(urls)
            assert r["added"] == 4
            ok = await wait_until(lambda: all(
                mgr.tasks[t].status == Status.COMPLETED for t in mgr.tasks
            ), timeout=30)
            assert ok
            for tid, n in zip(sorted(mgr.tasks), (1000, 2000, 3000, 4000)):
                assert _final_bytes(mgr, tid) == data(n)
    asyncio.run(main())


def test_content_disposition_filename(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/cd")
            await wait_status(mgr, tid, Status.COMPLETED)
            assert mgr.tasks[tid].filename == "报告 report.pdf"
            assert Path(mgr.tasks[tid].final_path).name == "报告 report.pdf"
    asyncio.run(main())


def test_redirect_followed(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/redirect/5000")
            await wait_status(mgr, tid, Status.COMPLETED)
            assert _final_bytes(mgr, tid) == data(5000)
    asyncio.run(main())


def test_dedupe_and_redownload(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            url = f"{server.url}/file/1000"
            r1 = await mgr.add_urls([url], dedupe=True)
            r2 = await mgr.add_urls([url], dedupe=True)
            assert r1["added"] == 1
            assert r2["added"] == 0 and r2["skipped"] == 1
            assert len(mgr.tasks) == 1
            # 关闭去重后允许重新下载
            r3 = await mgr.add_urls([url], dedupe=False)
            assert r3["added"] == 1
            assert len(mgr.tasks) == 2
    asyncio.run(main())


def test_conflict_rename(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            # 两个不同 URL 但同名（URL 基名都是 5000）
            r = await mgr.add_urls([
                f"{server.url}/file/5000?a=1",
                f"{server.url}/file/5000?a=2",
            ], dedupe=False)
            assert r["added"] == 2
            ok = await wait_until(lambda: all(
                mgr.tasks[t].status == Status.COMPLETED for t in mgr.tasks
            ), timeout=30)
            assert ok
            names = sorted(Path(mgr.tasks[t].final_path).name for t in mgr.tasks)
            assert names == ["5000", "5000 (1)"]
    asyncio.run(main())


def test_pause_resume_with_range(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            server.state["range_206"] = 0
            tid = await _add(mgr, f"{server.url}/slow/100000")
            await wait_partial(mgr, tid)
            await mgr.pause([tid])
            await wait_status(mgr, tid, Status.PAUSED)
            partial = mgr.tasks[tid].downloaded_bytes
            assert 0 < partial < 100000
            assert Path(mgr.tasks[tid].temp_path).exists()

            await mgr.resume([tid])
            await wait_status(mgr, tid, Status.COMPLETED)
            assert mgr.tasks[tid].resume_supported is True
            assert server.state["range_206"] >= 1, "续传应触发 206 响应"
            assert _final_bytes(mgr, tid) == data(100000)
    asyncio.run(main())


def test_norange_fallback_restarts(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/slow-norange/80000")
            await wait_partial(mgr, tid)
            await mgr.pause([tid])
            await wait_status(mgr, tid, Status.PAUSED)
            await mgr.resume([tid])
            await wait_status(mgr, tid, Status.COMPLETED)
            # 服务器忽略 Range，回退为重新下载
            assert mgr.tasks[tid].resume_supported is False
            assert _final_bytes(mgr, tid) == data(80000)
    asyncio.run(main())


def test_retry_on_429(tmp_path, server):
    async def main():
        async with running_manager(tmp_path, max_retries=3) as mgr:
            server.state["flaky"] = 0
            tid = await _add(mgr, f"{server.url}/flaky")
            await wait_status(mgr, tid, Status.COMPLETED, timeout=40)
            assert server.state["flaky"] >= 3  # 两次 429 + 一次成功
            assert mgr.tasks[tid].retry_count == 2
            assert _final_bytes(mgr, tid) == data(2000)
    asyncio.run(main())


def test_non_retryable_404_fails(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/status/404")
            await wait_status(mgr, tid, Status.FAILED)
            assert "404" in mgr.tasks[tid].error
            assert mgr.tasks[tid].retry_count == 0
    asyncio.run(main())


def test_incomplete_body_not_marked_success(tmp_path, server):
    async def main():
        async with running_manager(tmp_path, max_retries=0) as mgr:
            tid = await _add(mgr, f"{server.url}/liar")
            await wait_status(mgr, tid, Status.FAILED)
            assert mgr.tasks[tid].status != Status.COMPLETED
    asyncio.run(main())


def test_gzip_encoded_no_false_mismatch(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/gzip")
            await wait_status(mgr, tid, Status.COMPLETED)
            t = mgr.tasks[tid]
            assert t.total_bytes > 0
            assert t.downloaded_bytes == t.total_bytes
            # 保存的是原始字节，大小与 Content-Length 一致
            assert Path(t.final_path).stat().st_size == t.total_bytes
    asyncio.run(main())


def test_cancel_removes_temp(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/slow/100000")
            await wait_partial(mgr, tid)
            await mgr.cancel([tid])
            await wait_status(mgr, tid, Status.CANCELED)
            assert not Path(mgr.tasks[tid].temp_path).exists()
    asyncio.run(main())


def test_hook_rejects_private_target(tmp_path):
    # 直接验证 SSRF 钩子在内网关闭时拦截内网地址
    async def main():
        cfg = Config(tmp_path / "config.json")
        cfg.set("allow_private_network", False)
        mgr = DownloadManager(cfg, None, tmp_path)
        req = httpx.Request("GET", "http://127.0.0.1:1/x")
        with pytest.raises(SecurityError):
            await mgr._ssrf_guard(req)
        req2 = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
        with pytest.raises(SecurityError):
            await mgr._ssrf_guard(req2)

    asyncio.run(main())


def test_large_import(tmp_path):
    async def main():
        cfg = Config(tmp_path / "config.json")
        cfg.set("allow_private_network", True)
        db = Database(tmp_path / "data" / "tasks.db")
        await db.connect()
        mgr = DownloadManager(cfg, db, tmp_path)
        urls = [f"https://example.com/file/{i}.zip" for i in range(3000)]
        t0 = time.monotonic()
        r = await mgr.add_urls(urls)
        dt = time.monotonic() - t0
        assert r["added"] == 3000
        assert len(mgr.tasks) == 3000
        assert dt < 10.0, f"3000 条导入耗时 {dt:.2f}s，过慢"
        await db.close()
    asyncio.run(main())


def test_task_persistence(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            tid = await _add(mgr, f"{server.url}/file/7777")
            await wait_status(mgr, tid, Status.COMPLETED)
        # 重新打开数据库，任务应仍在
        db = Database(tmp_path / "data" / "tasks.db")
        await db.connect()
        tasks = await db.load_all()
        await db.close()
        assert len(tasks) == 1
        assert tasks[0].status == Status.COMPLETED
        assert tasks[0].downloaded_bytes == 7777
    asyncio.run(main())
