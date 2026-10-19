"""SQLite 任务持久化。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

import aiosqlite

from .models import PERSIST_FIELDS, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    filename TEXT NOT NULL DEFAULT '',
    filetype TEXT NOT NULL DEFAULT '',
    save_dir TEXT NOT NULL DEFAULT '',
    final_path TEXT NOT NULL DEFAULT '',
    temp_path TEXT NOT NULL DEFAULT '',
    total_bytes INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    source TEXT NOT NULL DEFAULT 'direct',
    batch_id TEXT NOT NULL DEFAULT '',
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    resume_supported INTEGER NOT NULL DEFAULT 0,
    chunk_state TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_url ON tasks(url);
"""

_COLS = ", ".join(PERSIST_FIELDS)
_PLACEHOLDERS = ", ".join("?" for _ in PERSIST_FIELDS)
_UPSERT = ", ".join(f"{c}=excluded.{c}" for c in PERSIST_FIELDS)


def _row_to_task(row) -> Task:
    t = Task()
    t.id = row["id"]
    for f in PERSIST_FIELDS:
        setattr(t, f, row[f])
    t.resume_supported = bool(t.resume_supported)
    return t


def _task_values(task: Task) -> list:
    vals = []
    for f in PERSIST_FIELDS:
        v = getattr(task, f)
        if f == "resume_supported":
            v = 1 if v else 0
        vals.append(v)
    return vals


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """为老库补新增列（幂等）。"""
        cur = await self.conn.execute("PRAGMA table_info(tasks)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "chunk_state" not in cols:
            await self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN chunk_state TEXT NOT NULL DEFAULT ''"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未连接")
        return self._conn

    async def insert(self, task: Task) -> int:
        cur = await self.conn.execute(
            f"INSERT INTO tasks ({_COLS}) VALUES ({_PLACEHOLDERS})",
            _task_values(task),
        )
        await self.conn.commit()
        task.id = cur.lastrowid
        return task.id

    async def insert_many(self, tasks: list[Task]) -> list[int]:
        """批量插入，单事务提交，返回与输入顺序对应的 id 列表。"""
        ids: list[int] = []
        for t in tasks:
            cur = await self.conn.execute(
                f"INSERT INTO tasks ({_COLS}) VALUES ({_PLACEHOLDERS})",
                _task_values(t),
            )
            ids.append(cur.lastrowid)
        await self.conn.commit()
        return ids

    async def save(self, task: Task) -> None:
        """UPSERT 单个任务（按 id）。"""
        await self.conn.execute(
            f"INSERT INTO tasks (id, {_COLS}) VALUES (?, {_PLACEHOLDERS}) "
            f"ON CONFLICT(id) DO UPDATE SET {_UPSERT}",
            [task.id, *_task_values(task)],
        )
        await self.conn.commit()

    async def save_many(self, tasks: Iterable[Task]) -> None:
        rows = [[t.id, *_task_values(t)] for t in tasks]
        await self.conn.executemany(
            f"INSERT INTO tasks (id, {_COLS}) VALUES (?, {_PLACEHOLDERS}) "
            f"ON CONFLICT(id) DO UPDATE SET {_UPSERT}",
            rows,
        )
        await self.conn.commit()

    async def load_all(self) -> list[Task]:
        cur = await self.conn.execute("SELECT * FROM tasks ORDER BY id ASC")
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def delete(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        qs = ",".join("?" for _ in ids)
        await self.conn.execute(f"DELETE FROM tasks WHERE id IN ({qs})", list(ids))
        await self.conn.commit()

    async def delete_all(self) -> None:
        await self.conn.execute("DELETE FROM tasks")
        await self.conn.commit()
