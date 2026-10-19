"""高速下载模式测试：开关、并发调度、大文件分块、回退、暂停恢复。"""
import asyncio
from pathlib import Path

from app.models import Status

from .helpers import data, running_manager, wait_partial, wait_status


async def _add(mgr, url, **kw):
    r = await mgr.add_urls([url], **kw)
    assert r["added"] == 1
    return max(mgr.tasks)


def _final_bytes(mgr, tid):
    return Path(mgr.tasks[tid].final_path).read_bytes()


def test_high_speed_toggles_concurrency(tmp_path):
    async def main():
        async with running_manager(tmp_path) as mgr:
            assert mgr._concurrency_limits() == (4, 4)
            mgr.config.set("high_speed", True)
            assert mgr._concurrency_limits() == (8, 4)
            mgr.config.set("high_speed_global_concurrency", 16)
            mgr.config.set("high_speed_per_domain_concurrency", 6)
            assert mgr._concurrency_limits() == (16, 6)
            mgr.config.set("high_speed", False)
            assert mgr._concurrency_limits() == (4, 4)
    asyncio.run(main())


def test_high_speed_off_uses_single_stream(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:  # high_speed 默认关闭
            server.state["range_206"] = 0
            tid = await _add(mgr, f"{server.url}/file/200000")
            await wait_status(mgr, tid, Status.COMPLETED)
            assert _final_bytes(mgr, tid) == data(200000)
            # 从头下载，无 Range 探测，不应产生 206
            assert server.state["range_206"] == 0
            assert mgr.tasks[tid].multi_chunk is False
    asyncio.run(main())


def test_multi_chunk_download_merges_correctly(tmp_path, server):
    async def main():
        async with running_manager(
            tmp_path, high_speed=True, chunk_threshold=1024 * 1024,
            max_chunks_per_file=4,
        ) as mgr:
            server.state["range_206"] = 0
            size = 3 * 1024 * 1024  # 3MB，threshold 1MB → 3 块
            tid = await _add(mgr, f"{server.url}/file/{size}")
            await wait_status(mgr, tid, Status.COMPLETED, timeout=60)
            t = mgr.tasks[tid]
            assert t.total_bytes == size
            assert t.downloaded_bytes == size
            assert _final_bytes(mgr, tid) == data(size)
            # 分块下载必然触发多次 206（探测 + 各块）
            assert server.state["range_206"] >= 3
    asyncio.run(main())


def test_chunk_fallback_when_no_range(tmp_path, server):
    async def main():
        async with running_manager(
            tmp_path, high_speed=True, chunk_threshold=1024 * 1024,
        ) as mgr:
            size = 2 * 1024 * 1024
            tid = await _add(mgr, f"{server.url}/norange/{size}")
            await wait_status(mgr, tid, Status.COMPLETED, timeout=60)
            t = mgr.tasks[tid]
            assert t.downloaded_bytes == size
            assert _final_bytes(mgr, tid) == data(size)
    asyncio.run(main())


def test_chunk_pause_resume(tmp_path, server):
    async def main():
        async with running_manager(
            tmp_path, high_speed=True, chunk_threshold=50 * 1024,
            max_chunks_per_file=4,
        ) as mgr:
            size = 200 * 1024  # 200KB → 4 块，慢速端点便于中途暂停
            tid = await _add(mgr, f"{server.url}/slow/{size}")
            await wait_partial(mgr, tid, timeout=30)
            assert mgr.tasks[tid].multi_chunk is True
            await mgr.pause([tid])
            await wait_status(mgr, tid, Status.PAUSED)
            assert mgr.tasks[tid].chunk_state, "暂停后应保留分块状态"
            # 分块临时文件应存在
            temp = Path(mgr.tasks[tid].temp_path)
            parts = list(temp.parent.glob(f"{temp.name}.*"))
            assert parts, "暂停后应保留分块临时文件"

            await mgr.resume([tid])
            await wait_status(mgr, tid, Status.COMPLETED, timeout=60)
            assert _final_bytes(mgr, tid) == data(size)
            # 分块临时文件合并后应清理
            parts = list(temp.parent.glob(f"{temp.name}.*"))
            assert not parts, "完成后分块临时文件应被清理"
    asyncio.run(main())


def test_small_file_no_chunking(tmp_path, server):
    async def main():
        async with running_manager(
            tmp_path, high_speed=True, chunk_threshold=1024 * 1024,
        ) as mgr:
            server.state["range_206"] = 0
            size = 100 * 1024  # 远小于阈值
            tid = await _add(mgr, f"{server.url}/file/{size}")
            await wait_status(mgr, tid, Status.COMPLETED)
            assert _final_bytes(mgr, tid) == data(size)
            # 小文件仅做 1 次 Range 探测（发现不够大后回退普通下载），无分块
            assert server.state["range_206"] == 1
            assert mgr.tasks[tid].multi_chunk is False
    asyncio.run(main())
