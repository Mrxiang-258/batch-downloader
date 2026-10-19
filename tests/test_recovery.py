"""任务持久化与重启恢复测试。"""
import asyncio
from pathlib import Path

from app.db import Database

from .helpers import data, make_config, running_manager, wait_partial, wait_status


def test_restart_recovery_and_resume(tmp_path, server):
    async def main():
        # 第一阶段：下载一个完成，另一个中途暂停（保留 .part）
        async with running_manager(tmp_path) as mgr:
            r = await mgr.add_urls([
                f"{server.url}/file/9000",
                f"{server.url}/slow/100000",
            ])
            assert r["added"] == 2
            ids = sorted(mgr.tasks)
            await wait_status(mgr, ids[0], "completed")
            await wait_partial(mgr, ids[1])
            await mgr.pause([ids[1]])
            await wait_status(mgr, ids[1], "paused")
            partial = mgr.tasks[ids[1]].downloaded_bytes
            assert 0 < partial < 100000
            assert Path(mgr.tasks[ids[1]].temp_path).exists()

        # 第二阶段：重启（同一数据库），任务应恢复
        async with running_manager(tmp_path) as mgr2:
            assert len(mgr2.tasks) == 2
            t0 = mgr2.tasks[ids[0]]
            t1 = mgr2.tasks[ids[1]]
            assert t0.status == "completed"
            assert t0.downloaded_bytes == 9000
            assert t1.status == "paused"
            assert t1.downloaded_bytes == partial

            await mgr2.resume([ids[1]])
            await wait_status(mgr2, ids[1], "completed")
            assert Path(t1.final_path).read_bytes() == data(100000)

    asyncio.run(main())


def test_downloading_reset_to_paused_on_start(tmp_path, server):
    """模拟异常退出：数据库中残留 downloading 状态，重启后应转为 paused。"""
    from app.models import Task, now_iso

    async def main():
        # 预置一条 status=downloading 的任务记录（模拟崩溃残留）
        cfg = make_config(tmp_path)
        db = Database(tmp_path / "data" / "tasks.db")
        await db.connect()
        t = Task(
            url="https://example.com/f.bin",
            status="downloading",
            filename="f.bin",
            filetype="bin",
            save_dir=str(tmp_path / "downloads"),
            temp_path=str(tmp_path / "downloads" / ".task1.part"),
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        t.id = await db.insert(t)
        await db.close()

        # 重启：downloading 应被重置为 paused，且可继续
        async with running_manager(tmp_path) as mgr:
            assert mgr.tasks[t.id].status == "paused"
            assert "中断" in (mgr.tasks[t.id].error or "")

    asyncio.run(main())
