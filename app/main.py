"""FastAPI 应用：REST API + SSE 实时推送 + 静态 Web UI。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .db import Database
from .engine import DownloadManager
from .models import Status
from .parser import extract_urls_from_text
from .security import SecurityError, validate_url

def _data_root() -> Path:
    # 打包成 exe 后，数据目录落在 exe 同级（便于用户查找）；源码运行时用项目根。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _data_root()
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------- 请求体模型 ----------

class AddTasksBody(BaseModel):
    urls: Optional[list[str]] = None
    text: Optional[str] = None
    save_dir: Optional[str] = None
    source: str = "direct"
    batch_id: str = ""
    dedupe: Optional[bool] = None


class IdsBody(BaseModel):
    ids: list[int] = []


class ParseBody(BaseModel):
    urls: list[str] = []


class OpenBody(BaseModel):
    path: str
    is_dir: bool = False


class ConfigBody(BaseModel):
    download_dir: Optional[str] = None
    global_concurrency: Optional[int] = None
    per_domain_concurrency: Optional[int] = None
    timeout: Optional[float] = None
    max_retries: Optional[int] = None
    chunk_size: Optional[int] = None
    dedupe: Optional[bool] = None
    conflict_policy: Optional[str] = None
    classify_by: Optional[str] = None
    high_speed: Optional[bool] = None
    high_speed_global_concurrency: Optional[int] = None
    high_speed_per_domain_concurrency: Optional[int] = None
    chunk_threshold: Optional[int] = None
    max_chunks_per_file: Optional[int] = None


_VALID_CONFLICT = {"rename", "overwrite", "skip"}
_VALID_CLASSIFY = {"none", "type", "domain", "batch"}


def _project_root() -> Path:
    # 允许通过环境变量覆盖，便于测试
    env = os.environ.get("DOWNLOADER_ROOT")
    return Path(env) if env else PROJECT_ROOT


def create_app(project_root: Optional[Path] = None) -> FastAPI:
    root = project_root or _project_root()
    config = Config(root / "config.json")
    db = Database(root / "data" / "tasks.db")
    manager = DownloadManager(config, db, root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        await manager.start()
        yield
        await manager.shutdown()
        await db.close()

    app = FastAPI(title="本地批量下载器", lifespan=lifespan)
    app.state.config = config
    app.state.db = db
    app.state.manager = manager
    app.state.root = root

    # ---------- 页面 ----------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ---------- 配置 ----------

    @app.get("/api/config")
    async def get_config() -> dict:
        return config.public_view(root)

    @app.post("/api/config")
    async def update_config(body: ConfigBody) -> dict:
        updates: dict[str, Any] = {}
        if body.download_dir is not None:
            updates["download_dir"] = str(body.download_dir).strip()
        if body.global_concurrency is not None:
            updates["global_concurrency"] = _clamp_int(body.global_concurrency, 1, 64)
        if body.per_domain_concurrency is not None:
            updates["per_domain_concurrency"] = _clamp_int(body.per_domain_concurrency, 1, 32)
        if body.timeout is not None:
            updates["timeout"] = _clamp_float(body.timeout, 1.0, 600.0)
        if body.max_retries is not None:
            updates["max_retries"] = _clamp_int(body.max_retries, 0, 20)
        if body.chunk_size is not None:
            updates["chunk_size"] = _clamp_int(body.chunk_size, 4096, 1024 * 1024)
        if body.dedupe is not None:
            updates["dedupe"] = bool(body.dedupe)
        if body.conflict_policy is not None:
            if body.conflict_policy not in _VALID_CONFLICT:
                raise HTTPException(400, "conflict_policy 取值非法")
            updates["conflict_policy"] = body.conflict_policy
        if body.classify_by is not None:
            if body.classify_by not in _VALID_CLASSIFY:
                raise HTTPException(400, "classify_by 取值非法")
            updates["classify_by"] = body.classify_by
        if body.high_speed is not None:
            updates["high_speed"] = bool(body.high_speed)
        if body.high_speed_global_concurrency is not None:
            updates["high_speed_global_concurrency"] = _clamp_int(
                body.high_speed_global_concurrency, 1, 32
            )
        if body.high_speed_per_domain_concurrency is not None:
            updates["high_speed_per_domain_concurrency"] = _clamp_int(
                body.high_speed_per_domain_concurrency, 1, 8
            )
        if body.chunk_threshold is not None:
            updates["chunk_threshold"] = _clamp_int(
                body.chunk_threshold, 1024 * 1024, 1024 * 1024 * 1024
            )
        if body.max_chunks_per_file is not None:
            updates["max_chunks_per_file"] = _clamp_int(body.max_chunks_per_file, 2, 8)
        config.update(updates)
        config.save()
        return config.public_view(root)

    # ---------- 任务 ----------

    @app.get("/api/tasks")
    async def list_tasks() -> dict:
        tasks = [t.to_dict() for t in manager.tasks.values()]
        return {"tasks": tasks, "summary": _summary(manager)}

    @app.post("/api/tasks")
    async def add_tasks(body: AddTasksBody) -> dict:
        urls: list[str] = []
        if body.urls:
            urls.extend(u for u in body.urls if u and u.strip())
        if body.text:
            urls.extend(extract_urls_from_text(body.text))
        if not urls:
            raise HTTPException(400, "未提供有效 URL")
        # 校验每条 URL（非法/内网地址直接拒绝并给出原因）
        allow_private = bool(config.get("allow_private_network", False))
        for u in urls:
            try:
                validate_url(u, allow_private=allow_private)
            except SecurityError as e:
                raise HTTPException(400, str(e)) from e
        result = await manager.add_urls(
            urls, save_dir=body.save_dir, source=body.source,
            batch_id=body.batch_id, dedupe=body.dedupe,
        )
        return {"summary": _summary(manager), **result}

    @app.post("/api/tasks/pause")
    async def pause(body: IdsBody) -> dict:
        await manager.pause(body.ids)
        return _ok()

    @app.post("/api/tasks/resume")
    async def resume(body: IdsBody) -> dict:
        await manager.resume(body.ids)
        return _ok()

    @app.post("/api/tasks/cancel")
    async def cancel(body: IdsBody) -> dict:
        await manager.cancel(body.ids)
        return _ok()

    @app.post("/api/tasks/retry")
    async def retry(body: IdsBody) -> dict:
        n = await manager.retry_failed(body.ids or None)
        return {"retried": n}

    @app.post("/api/tasks/clear")
    async def clear(body: IdsBody) -> dict:
        await manager.clear(body.ids)
        return _ok()

    @app.post("/api/tasks/clear_completed")
    async def clear_completed() -> dict:
        await manager.clear_completed()
        return _ok()

    @app.post("/api/tasks/pause_all")
    async def pause_all() -> dict:
        await manager.pause_all()
        return _ok()

    @app.post("/api/tasks/resume_all")
    async def resume_all() -> dict:
        await manager.resume_all()
        return _ok()

    @app.post("/api/tasks/cancel_all")
    async def cancel_all() -> dict:
        await manager.cancel_all()
        return _ok()

    # ---------- 网页解析 ----------

    @app.post("/api/parse")
    async def parse_pages(body: ParseBody) -> dict:
        results = []
        allow_private = bool(config.get("allow_private_network", False))
        for url in body.urls[:20]:
            try:
                validate_url(url, allow_private=allow_private)
                results.append(await manager.parse_page(url))
            except SecurityError as e:
                results.append({"url": url, "is_html": False, "links": [], "error": str(e)})
            except Exception as e:  # noqa: BLE001
                results.append({"url": url, "is_html": False, "links": [], "error": f"解析失败：{e}"})
        return {"results": results}

    # ---------- 打开文件/目录 ----------

    @app.post("/api/open")
    async def open_path(body: OpenBody) -> dict:
        try:
            manager.open_path(body.path, body.is_dir)
            return _ok()
        except SecurityError as e:
            raise HTTPException(400, str(e)) from e

    # ---------- 文件夹选择器（Windows 原生） ----------

    @app.post("/api/browse_folder")
    async def browse_folder() -> dict:
        if os.name != "nt":
            raise HTTPException(400, "文件夹选择器仅支持 Windows")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = '选择下载目录'; "
            "$f.ShowNewFolderButton = $true; "
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::Out.Write($f.SelectedPath) }"
        )
        loop = asyncio.get_running_loop()

        def _run() -> tuple[str, int]:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, text=True, timeout=300,
            )
            return proc.stdout.strip(), proc.returncode

        try:
            out, code = await loop.run_in_executor(None, _run)
        except subprocess.TimeoutExpired:
            return {"path": None}
        if code == 0 and out and Path(out).is_dir():
            return {"path": str(out)}
        return {"path": None}

    # ---------- SSE ----------

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        q = manager.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                manager.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def _summary(manager: DownloadManager) -> dict[str, int]:
    counts = {
        Status.PENDING: 0, Status.DOWNLOADING: 0, Status.PAUSED: 0,
        Status.COMPLETED: 0, Status.FAILED: 0, Status.CANCELED: 0,
        Status.SKIPPED: 0,
    }
    for t in manager.tasks.values():
        counts[t.status] = counts.get(t.status, 0) + 1
    counts["total"] = len(manager.tasks)
    counts["active"] = len(manager._active)
    counts["active_connections"] = len(manager._active) + manager._active_chunks
    counts["high_speed"] = bool(manager.config.get("high_speed", False))
    return counts


def _ok() -> dict:
    return {"ok": True}


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _clamp_float(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


# uvicorn 入口：uvicorn app.main:app
app = create_app()
