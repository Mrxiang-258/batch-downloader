"""异步下载引擎：任务队列、并发调度、断点续传、重试、暂停/恢复/取消、高速分块下载。"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .config import Config
from .db import Database
from .fileutils import (
    build_filename,
    filename_from_content_disposition,
    filename_from_url,
    guess_category,
    guess_extension,
)
from .models import Status, Task, now_iso
from .parser import extract_links
from .resource import (
    CATEGORY_UNKNOWN,
    STATUS_AUTH,
    STATUS_AVAILABLE,
    STATUS_INACCESSIBLE,
    STATUS_UNKNOWN,
    Resource,
    classify as classify_resource,
    domain_of,
    normalize_url,
)
from .security import (
    SecurityError,
    path_is_under,
    safe_join,
    sanitize_filename,
    validate_url,
)


class DownloadError(Exception):
    """下载过程中的错误；retryable 决定是否进入重试。"""

    def __init__(self, message: str, retryable: bool = False, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class _Paused(Exception):
    pass


class _Cancelled(Exception):
    pass


class _ChunkFallback(Exception):
    """分块下载不适用或失败，回退普通下载。"""


def _domain_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


# 网页解析时单次最多探测的资源数量（避免无节制探测）
MAX_PROBE_RESOURCES = 100
# 网页解析时资源探测的有限并发数
PROBE_CONCURRENCY = 8


class DownloadManager:
    def __init__(
        self,
        config: Config,
        db: Database,
        project_root: Path,
        fetch_html: bool = True,
    ):
        self.config = config
        self.db = db
        self.project_root = project_root
        self.tasks: dict[int, Task] = {}
        self._active: set[int] = set()          # 当前正在执行的 worker 任务 id
        self._pause_flags: dict[int, bool] = {}
        self._cancel_flags: dict[int, bool] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._wake = asyncio.Event()
        self._scheduler_task: Optional[asyncio.Task] = None
        self._running = False
        self._client: Optional[httpx.AsyncClient] = None
        self._speed_ts: dict[int, tuple[float, int]] = {}
        self._workers: set[asyncio.Task] = set()   # 正在运行的下载 worker
        self._commit_lock = asyncio.Lock()          # 序列化最终文件提交，避免重名竞态
        self._active_chunks = 0                     # 分块下载的额外活动连接数（统计用）

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        g = max(1, int(self.config.get("global_concurrency", 4)))
        hg = max(1, int(self.config.get("high_speed_global_concurrency", 8)))
        max_chunks = max(1, int(self.config.get("max_chunks_per_file", 4)))
        # 连接池上限需覆盖高速并发 + 分块连接
        max_conn = max(10, max(g, hg) * (max_chunks + 1))
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                float(self.config.get("timeout", 30)),
                connect=float(self.config.get("timeout", 30)),
            ),
            headers={"User-Agent": str(self.config.get("user_agent", "")),
                     "Accept-Encoding": "identity"},
            event_hooks={"request": [self._ssrf_guard]},
            limits=httpx.Limits(
                max_connections=max_conn,
                max_keepalive_connections=max(4, max(g, hg)),
            ),
        )
        # 从数据库恢复任务
        for task in await self.db.load_all():
            self.tasks[task.id] = task
        # 上次中断的“下载中”任务重置为“已暂停”，保留 .part 以便续传
        changed = False
        for task in self.tasks.values():
            if task.status == Status.DOWNLOADING:
                task.status = Status.PAUSED
                task.error = "上次运行中断，可继续下载"
                task.updated_at = now_iso()
                changed = True
        if changed:
            await self.db.save_many(
                [t for t in self.tasks.values() if t.status == Status.PAUSED]
            )
        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler())

    async def shutdown(self) -> None:
        self._running = False
        self._wake.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        # 记录仍在下载的任务，取消并等待所有 worker 退出
        active = [self.tasks[t] for t in list(self._active) if t in self.tasks]
        for w in list(self._workers):
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        # 将仍在下载中的任务标记为暂停（保留 .part 供下次续传）
        for t in active:
            if t.status == Status.DOWNLOADING:
                t.status = Status.PAUSED
                t.error = "应用已退出，可继续下载"
                t.updated_at = now_iso()
        if active:
            await self.db.save_many(active)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ssrf_guard(self, request: httpx.Request) -> None:
        # 每次请求（含重定向后的每一跳）都重新校验 URL
        validate_url(
            str(request.url),
            allow_private=bool(self.config.get("allow_private_network", False)),
        )

    # ---------- 事件广播 ----------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, payload: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 慢消费者丢弃事件，前端可随时 REST 拉全量

    async def _emit_task(self, task: Task) -> None:
        self._broadcast({"type": "task", "task": task.to_dict()})

    # ---------- 任务添加 ----------

    async def add_urls(
        self,
        urls: Iterable[str],
        save_dir: Optional[str] = None,
        source: str = "direct",
        batch_id: str = "",
        dedupe: Optional[bool] = None,
    ) -> dict[str, Any]:
        if dedupe is None:
            dedupe = bool(self.config.get("dedupe", True))

        existing = {t.url for t in self.tasks.values()} if dedupe else set()
        max_retries = int(self.config.get("max_retries", 3))
        default_dir = str(self.config.resolved_download_dir(self.project_root))
        target_dir = (save_dir or "").strip() or default_dir
        target_dir = str(Path(target_dir).expanduser())

        added, skipped = 0, 0
        new_tasks: list[Task] = []
        for url in urls:
            url = (url or "").strip()
            if not url:
                continue
            if dedupe and url in existing:
                skipped += 1
                continue
            existing.add(url)
            task = Task(
                url=url,
                save_dir=target_dir,
                source=source,
                batch_id=batch_id,
                max_retries=max_retries,
                filename=sanitize_filename(self._guess_name(url)),
                filetype=guess_extension(url),
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            new_tasks.append(task)
            added += 1

        if new_tasks:
            ids = await self.db.insert_many(new_tasks)
            for task, tid in zip(new_tasks, ids):
                task.id = tid
                self.tasks[tid] = task

        self._wake.set()
        return {"added": added, "skipped": skipped}

    @staticmethod
    def _guess_name(url: str) -> str:
        from .fileutils import filename_from_url
        return filename_from_url(url) or "download"

    # ---------- 网页解析 ----------

    async def parse_page(self, url: str) -> dict[str, Any]:
        validate_url(
            url, allow_private=bool(self.config.get("allow_private_network", False))
        )
        resp = await self._get_client().get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return {
                "url": url,
                "is_html": False,
                "resources": [],
                "categories": {},
                "note": f"目标不是 HTML 页面（Content-Type: {ctype or '未知'}），无法解析链接",
            }
        links = extract_links(resp.text, str(resp.url))
        downloadable = [l for l in links if l["downloadable"]]

        # 去重（规范化 URL，保留查询参数中的签名/令牌）
        seen: dict[str, dict] = {}
        for l in downloadable:
            key = normalize_url(l["url"])
            if key and key not in seen:
                seen[key] = l
        candidates = list(seen.values())[:MAX_PROBE_RESOURCES]

        # 有限并发探测资源元数据
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        results = await asyncio.gather(
            *(self._probe_resource(l["url"], str(resp.url), sem) for l in candidates)
        )
        resources = [r for r in results if r is not None]

        categories: dict[str, int] = {}
        for r in resources:
            categories[r.category] = categories.get(r.category, 0) + 1

        return {
            "url": str(resp.url),
            "is_html": True,
            "total_links": len(links),
            "downloadable_count": len(downloadable),
            "resource_count": len(resources),
            "categories": categories,
            "resources": [r.to_dict() for r in resources],
            "truncated": len(candidates) < len(seen),
        }

    async def _probe_resource(
        self, url: str, source_url: str, sem: asyncio.Semaphore
    ) -> Optional[Resource]:
        """探测单个资源元数据（HEAD，失败时降级 GET Range），返回 Resource。"""
        async with sem:
            client = self._get_client()
            allow_private = bool(self.config.get("allow_private_network", False))
            try:
                validate_url(url, allow_private=allow_private)
            except SecurityError as e:
                return Resource(
                    url=url, status=STATUS_INACCESSIBLE,
                    source_url=source_url, source_domain=domain_of(source_url),
                    note=str(e),
                )

            head = None
            try:
                head = await client.head(url)
                if head.status_code in (401, 403):
                    return Resource(
                        url=url, status=STATUS_AUTH,
                        source_url=source_url, source_domain=domain_of(source_url),
                        note=f"HTTP {head.status_code}，可能需要授权",
                    )
                if head.status_code >= 400 and head.status_code not in (405, 501):
                    return Resource(
                        url=url, status=STATUS_INACCESSIBLE,
                        source_url=source_url, source_domain=domain_of(source_url),
                        note=f"HTTP {head.status_code}",
                    )
                if head.status_code in (405, 501):
                    head = None  # HEAD 不支持，改用 GET Range
            except (httpx.HTTPError, SecurityError):
                head = None

            if head is not None:
                mime = head.headers.get("content-type", "")
                size = _parse_content_length(head.headers.get("content-length", ""))
                cd = head.headers.get("content-disposition", "")
                return self._build_resource(url, mime, size, cd, source_url)

            # HEAD 不可用：轻量 GET Range: bytes=0-0
            try:
                async with client.stream(
                    "GET", url, headers={"Range": "bytes=0-0"}
                ) as r:
                    if r.status_code in (401, 403):
                        return Resource(
                            url=url, status=STATUS_AUTH,
                            source_url=source_url, source_domain=domain_of(source_url),
                            note=f"HTTP {r.status_code}，可能需要授权",
                        )
                    if r.status_code >= 400:
                        return Resource(
                            url=url, status=STATUS_INACCESSIBLE,
                            source_url=source_url, source_domain=domain_of(source_url),
                            note=f"HTTP {r.status_code}",
                        )
                    mime = r.headers.get("content-type", "")
                    size = (
                        _parse_content_range_total(r.headers.get("content-range", ""))
                        or _parse_content_length(r.headers.get("content-length", ""))
                    )
                    cd = r.headers.get("content-disposition", "")
            except (httpx.HTTPError, SecurityError) as e:
                return Resource(
                    url=url, status=STATUS_INACCESSIBLE,
                    source_url=source_url, source_domain=domain_of(source_url),
                    note=f"探测失败：{e}",
                )
            return self._build_resource(url, mime, size, cd, source_url)

    @staticmethod
    def _build_resource(
        url: str, mime: str, size: int, cd: str, source_url: str
    ) -> Resource:
        mime_type = (mime or "").split(";")[0].strip().lower()
        cd_name = filename_from_content_disposition(cd) if cd else ""
        if mime_type == "text/html":
            return Resource(
                url=url,
                filename=filename_from_url(url) or "download",
                category=CATEGORY_UNKNOWN,
                mime=mime_type,
                source_url=source_url,
                source_domain=domain_of(source_url),
                status=STATUS_INACCESSIBLE,
                note="响应为网页，非文件",
            )
        cat, filetype, filename, note = classify_resource(url, mime, cd_name)
        status = STATUS_UNKNOWN if note else STATUS_AVAILABLE
        return Resource(
            url=url, filename=filename, filetype=filetype, category=cat,
            size=size, mime=mime_type, source_url=source_url,
            source_domain=domain_of(source_url), status=status, note=note,
        )

    # ---------- 控制命令 ----------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("下载器尚未启动")
        return self._client

    async def pause(self, ids: list[int]) -> None:
        for tid in ids:
            if tid in self.tasks and self.tasks[tid].status == Status.DOWNLOADING:
                self._pause_flags[tid] = True
            elif tid in self.tasks and self.tasks[tid].status == Status.PENDING:
                self.tasks[tid].status = Status.PAUSED
                self.tasks[tid].updated_at = now_iso()
                await self.db.save(self.tasks[tid])
                await self._emit_task(self.tasks[tid])

    async def resume(self, ids: list[int]) -> None:
        for tid in ids:
            t = self.tasks.get(tid)
            if t is None:
                continue
            if t.status in (Status.PAUSED, Status.FAILED, Status.CANCELED, Status.SKIPPED):
                self._pause_flags[tid] = False
                self._cancel_flags[tid] = False
                t.status = Status.PENDING
                t.error = ""
                t.retry_count = 0
                t.updated_at = now_iso()
                await self.db.save(t)
                await self._emit_task(t)
        self._wake.set()

    async def cancel(self, ids: list[int]) -> None:
        for tid in ids:
            t = self.tasks.get(tid)
            if t is None:
                continue
            if t.status in (Status.DOWNLOADING, Status.PENDING):
                self._cancel_flags[tid] = True
                self._pause_flags[tid] = False
            elif t.status == Status.PAUSED:
                t.status = Status.CANCELED
                t.updated_at = now_iso()
                await self.db.save(t)
                await self._emit_task(t)
                self._remove_temp(t)

    async def retry_failed(self, ids: Optional[list[int]] = None) -> int:
        targets = ids if ids else [
            tid for tid, t in self.tasks.items() if t.status == Status.FAILED
        ]
        for tid in targets:
            t = self.tasks.get(tid)
            if t is not None and t.status == Status.FAILED:
                t.status = Status.PENDING
                t.error = ""
                t.retry_count = 0
                t.updated_at = now_iso()
                await self.db.save(t)
                await self._emit_task(t)
        self._wake.set()
        return len(targets)

    async def pause_all(self) -> None:
        await self.pause(list(self.tasks.keys()))

    async def resume_all(self) -> None:
        await self.resume([
            tid for tid, t in self.tasks.items()
            if t.status in (Status.PAUSED, Status.FAILED)
        ])

    async def cancel_all(self) -> None:
        await self.cancel([
            tid for tid, t in self.tasks.items()
            if t.status in (Status.PENDING, Status.DOWNLOADING, Status.PAUSED)
        ])

    async def clear(self, ids: list[int]) -> None:
        for tid in ids:
            t = self.tasks.get(tid)
            if t is None:
                continue
            if t.status in (Status.COMPLETED, Status.FAILED, Status.CANCELED, Status.SKIPPED):
                self._remove_temp(t)
                del self.tasks[tid]
        await self.db.delete(ids)

    async def clear_completed(self) -> None:
        ids = [tid for tid, t in self.tasks.items() if t.status == Status.COMPLETED]
        await self.clear(ids)

    # ---------- 调度器 ----------

    async def _scheduler(self) -> None:
        while self._running:
            try:
                self._launch_available()
            except Exception:  # noqa: BLE001 - 调度器永不因单次异常退出
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

    def _concurrency_limits(self) -> tuple[int, int]:
        """按高速模式返回 (全局并发上限, 单域名并发上限)。"""
        if self.config.get("high_speed", False):
            return (
                max(1, min(32, int(self.config.get("high_speed_global_concurrency", 8)))),
                max(1, min(8, int(self.config.get("high_speed_per_domain_concurrency", 4)))),
            )
        return (
            max(1, int(self.config.get("global_concurrency", 4))),
            max(1, int(self.config.get("per_domain_concurrency", 2))),
        )

    def _launch_available(self) -> None:
        global_limit, per_domain = self._concurrency_limits()
        if len(self._active) >= global_limit:
            return
        running_by_domain: Counter = Counter(
            _domain_of(self.tasks[t].url) for t in self._active if t in self.tasks
        )
        for task in self.tasks.values():
            if len(self._active) >= global_limit:
                break
            if task.status != Status.PENDING:
                continue
            d = _domain_of(task.url)
            if running_by_domain[d] >= per_domain:
                continue
            self._active.add(task.id)
            running_by_domain[d] += 1
            worker = asyncio.create_task(self._run_task(task))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _run_task(self, task: Task) -> None:
        try:
            await self._download_with_retries(task)
        finally:
            self._active.discard(task.id)
            self._speed_ts.pop(task.id, None)
            self._wake.set()

    # ---------- 下载执行 ----------

    async def _download_with_retries(self, task: Task) -> None:
        task.status = Status.DOWNLOADING
        task.error = ""
        task.started_at = now_iso()
        task.updated_at = now_iso()
        await self.db.save(task)
        await self._emit_task(task)

        try:
            self._prepare_paths(task)
        except SecurityError as e:
            await self._finish_failed(task, str(e))
            return

        # skip 策略且目标已存在
        if (
            self.config.get("conflict_policy") == "skip"
            and task.final_path
            and Path(task.final_path).exists()
        ):
            task.status = Status.SKIPPED
            task.error = "目标文件已存在（策略：跳过）"
            await self._finish_terminal(task)
            return

        attempt = 0
        while True:
            try:
                if self._should_try_chunks(task):
                    try:
                        await self._stream_multi_chunk(task)
                    except _ChunkFallback:
                        self._cleanup_chunks(task)
                        await self._stream_once(task)
                else:
                    await self._stream_once(task)
                self._validate_size(task)
                result = await self._commit(task)
                if result == "skipped":
                    task.status = Status.SKIPPED
                    task.error = "目标文件已存在（策略：跳过）"
                    self._remove_temp(task)
                    await self._finish_terminal(task)
                    return
                task.status = Status.COMPLETED
                task.error = ""
                task.completed_at = now_iso()
                await self._finish_terminal(task)
                return
            except _Paused:
                task.status = Status.PAUSED
                await self._finish_terminal(task)
                return
            except _Cancelled:
                task.status = Status.CANCELED
                task.error = "已取消"
                self._remove_temp(task)
                await self._finish_terminal(task)
                return
            except DownloadError as e:
                if e.retryable and attempt < task.max_retries:
                    attempt += 1
                    task.retry_count = attempt
                    task.error = f"重试 {attempt}/{task.max_retries}: {e}"
                    task.updated_at = now_iso()
                    await self.db.save(task)
                    await self._emit_task(task)
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                await self._finish_failed(task, str(e))
                return
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < task.max_retries:
                    attempt += 1
                    task.retry_count = attempt
                    task.error = f"网络错误，重试 {attempt}/{task.max_retries}: {e}"
                    task.updated_at = now_iso()
                    await self.db.save(task)
                    await self._emit_task(task)
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                await self._finish_failed(task, f"网络错误：{e}")
                return
            except SecurityError as e:
                await self._finish_failed(task, str(e))
                return
            except Exception as e:  # noqa: BLE001
                await self._finish_failed(task, f"未知错误：{e}")
                return

    # ---------- 高速分块下载 ----------

    def _should_try_chunks(self, task: Task) -> bool:
        """判断是否尝试分块下载（仅高速模式，且非普通续传）。"""
        if not self.config.get("high_speed", False):
            return False
        if task.chunk_state:  # 之前已分块，继续分块
            return True
        if task.temp_path:
            try:
                p = Path(task.temp_path)
                if p.exists() and p.stat().st_size > 0:
                    return False  # 已有普通下载的部分数据，续传
            except OSError:
                pass
        return True

    @staticmethod
    def _load_chunk_state(task: Task) -> Optional[dict]:
        if not task.chunk_state:
            return None
        try:
            d = json.loads(task.chunk_state)
            if isinstance(d, dict) and int(d.get("total", 0)) > 0:
                return d
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _save_chunk_state(task: Task, st: dict) -> None:
        task.chunk_state = json.dumps(st)

    async def _probe_range(self, task: Task) -> Optional[dict]:
        """探测文件大小、Range 支持、验证器。返回信息 dict 或 None（不支持分块）。"""
        client = self._get_client()
        try:
            async with client.stream("GET", task.url, headers={"Range": "bytes=0-0"}) as r:
                if r.status_code != 206:
                    return None
                if (r.headers.get("content-encoding", "") or "").strip():
                    return None
                total = _parse_content_range_total(r.headers.get("content-range", ""))
                if total <= 0:
                    return None
                return {
                    "total": total,
                    "etag": r.headers.get("etag", "") or "",
                    "last_modified": r.headers.get("last-modified", "") or "",
                }
        except (httpx.HTTPError, SecurityError):
            return None

    async def _stream_multi_chunk(self, task: Task) -> None:
        """多连接分块下载：写入 .part.{i} 临时分块，全部完成后合并。"""
        threshold = max(1, int(self.config.get("chunk_threshold", 10 * 1024 * 1024)))
        max_chunks = max(1, int(self.config.get("max_chunks_per_file", 4)))

        st = self._load_chunk_state(task)
        if st is None:
            info = await self._probe_range(task)
            if info is None or info["total"] < threshold:
                raise _ChunkFallback()
            if not info["etag"] and not info["last_modified"]:
                raise _ChunkFallback()
            num = min(max_chunks, info["total"] // threshold)
            if num < 2:
                raise _ChunkFallback()
            st = {
                "total": num,
                "file_size": int(info["total"]),
                "etag": info["etag"],
                "last_modified": info["last_modified"],
            }
            task.etag = info["etag"] or task.etag
            task.last_modified = info["last_modified"] or task.last_modified
            self._save_chunk_state(task, st)
            await self.db.save(task)

        total = int(st["file_size"])
        num = int(st["total"])
        chunk_len = (total + num - 1) // num
        task.total_bytes = total
        task.file_size = total
        task.multi_chunk = True
        task.resume_supported = True
        temp = Path(task.temp_path)

        # 恢复时重算已下载字节
        task.downloaded_bytes = 0
        for i in range(num):
            part = Path(f"{temp}.{i}")
            if part.exists():
                task.downloaded_bytes += part.stat().st_size

        async def _download_chunk(i: int) -> None:
            start = i * chunk_len
            end = min((i + 1) * chunk_len - 1, total - 1)
            part = Path(f"{temp}.{i}")
            local = part.stat().st_size if part.exists() else 0
            if local >= (end - start + 1):
                return  # 该块已完成
            # Connection: close 避免分块并发下复用残留连接导致响应串流
            headers = {"Range": f"bytes={start + local}-{end}", "Connection": "close"}
            if task.etag:
                headers["If-Range"] = task.etag
            elif task.last_modified:
                headers["If-Range"] = task.last_modified
            fh = open(part, "ab")
            got = 0
            try:
                async with self._get_client().stream(
                    "GET", task.url, headers=headers
                ) as resp:
                    if resp.status_code not in (200, 206):
                        raise DownloadError(
                            f"分块 {i} 失败：HTTP {resp.status_code}",
                            retryable=resp.status_code in (408, 429, 500, 502, 503, 504),
                            status=resp.status_code,
                        )
                    if resp.status_code == 200:
                        # 服务器忽略 Range（文件可能已变化），回退普通下载
                        raise _ChunkFallback()
                    # 用较小的读取粒度，让进度/速度反馈更实时
                    chunk_size = 16 * 1024
                    async for chunk in resp.aiter_raw(chunk_size):
                        if self._cancel_flags.get(task.id):
                            raise _Cancelled()
                        if self._pause_flags.get(task.id):
                            raise _Paused()
                        fh.write(chunk)
                        got += len(chunk)
                        task.downloaded_bytes += len(chunk)
                expected = end - (start + local) + 1
                if got != expected:
                    raise DownloadError(
                        f"分块 {i} 长度不符：期望 {expected}，实际 {got}"
                    )
            finally:
                fh.close()

        done = asyncio.Event()

        async def _speed_sampler() -> None:
            last_bytes = task.downloaded_bytes
            last_ts = time.monotonic()
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                dt = now - last_ts
                if dt > 0:
                    db_ = task.downloaded_bytes - last_bytes
                    task.speed = db_ / dt
                    task.eta = (
                        (task.total_bytes - task.downloaded_bytes) / task.speed
                        if task.speed > 0 and task.total_bytes > 0 else 0.0
                    )
                last_bytes, last_ts = task.downloaded_bytes, now
                task.updated_at = now_iso()
                await self.db.save(task)
                await self._emit_task(task)

        sampler = asyncio.create_task(_speed_sampler())
        try:
            self._active_chunks += num
            await asyncio.gather(*(_download_chunk(i) for i in range(num)))
            await asyncio.to_thread(self._merge_chunks, temp, num)
            task.downloaded_bytes = total
            task.chunk_state = ""
        finally:
            self._active_chunks -= num
            task.multi_chunk = False
            done.set()
            if not sampler.done():
                sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)

    @staticmethod
    def _merge_chunks(temp: Path, num: int) -> None:
        """按顺序合并分块文件为最终临时文件，并删除分块文件。"""
        with open(temp, "wb") as out:
            for i in range(num):
                part = Path(f"{temp}.{i}")
                with open(part, "rb") as f:
                    while True:
                        buf = f.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
        for i in range(num):
            try:
                Path(f"{temp}.{i}").unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_chunks(self, task: Task) -> None:
        """分块失败回退时，清理分块文件与状态。"""
        task.chunk_state = ""
        task.multi_chunk = False
        if not task.temp_path:
            return
        temp = Path(task.temp_path)
        for i in range(64):
            part = Path(f"{temp}.{i}")
            if not part.exists():
                break
            try:
                part.unlink()
            except OSError:
                pass

    async def _stream_once(self, task: Task) -> None:
        """执行一次下载（含断点续传），写入临时文件。"""
        temp = Path(task.temp_path)
        resume_from = 0
        if temp.exists():
            resume_from = temp.stat().st_size
            task.downloaded_bytes = resume_from

        headers: dict[str, str] = {}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            if task.etag:
                headers["If-Range"] = task.etag
            elif task.last_modified:
                headers["If-Range"] = task.last_modified

        mode = "ab" if resume_from > 0 else "wb"
        fh = open(temp, mode)
        offset = resume_from
        try:
            async with self._get_client().stream("GET", task.url, headers=headers) as resp:
                status = resp.status_code
                if status not in (200, 206):
                    raise DownloadError(
                        f"HTTP {status}：{_http_reason(resp)}",
                        retryable=status in (408, 429, 500, 502, 503, 504),
                        status=status,
                    )

                if status == 206:
                    task.resume_supported = True
                    total = _parse_content_range_total(resp.headers.get("content-range", ""))
                    if total:
                        task.total_bytes = total
                else:  # 200：不支持续传或文件已变化，重新开始
                    if offset > 0:
                        task.resume_supported = False
                        fh.close()
                        fh = open(temp, "wb")
                        offset = 0
                    task.downloaded_bytes = 0
                    task.total_bytes = _parse_content_length(resp.headers.get("content-length", ""))

                task.etag = resp.headers.get("etag", "") or task.etag
                task.last_modified = resp.headers.get("last-modified", "") or task.last_modified

                # 依据响应头解析文件名
                cd = resp.headers.get("content-disposition", "")
                if cd or not task.filename or task.filename == "download":
                    task.filename = build_filename(task.url, cd)
                    task.filetype = guess_extension(task.filename)
                    self._resolve_final_path(task)

                task.file_size = task.total_bytes

                chunk_size = int(self.config.get("chunk_size", 64 * 1024))
                last_ts = time.monotonic()
                last_bytes = task.downloaded_bytes
                async for chunk in resp.aiter_raw(chunk_size):
                    if self._cancel_flags.get(task.id):
                        raise _Cancelled()
                    if self._pause_flags.get(task.id):
                        raise _Paused()
                    fh.write(chunk)
                    offset += len(chunk)
                    task.downloaded_bytes = offset
                    now = time.monotonic()
                    if now - last_ts >= 1.0:
                        dt = now - last_ts
                        db_ = offset - last_bytes
                        task.speed = db_ / dt if dt > 0 else 0.0
                        if task.speed > 0 and task.total_bytes > 0:
                            task.eta = (task.total_bytes - offset) / task.speed
                        else:
                            task.eta = 0.0
                        last_ts, last_bytes = now, offset
                        task.updated_at = now_iso()
                        await self.db.save(task)
                        await self._emit_task(task)
        finally:
            fh.close()

        if self._cancel_flags.get(task.id):
            raise _Cancelled()
        if self._pause_flags.get(task.id):
            raise _Paused()

    def _validate_size(self, task: Task) -> None:
        if task.total_bytes > 0 and task.downloaded_bytes != task.total_bytes:
            raise DownloadError(
                f"文件大小校验失败：期望 {task.total_bytes} 字节，实际 {task.downloaded_bytes} 字节"
            )

    async def _commit(self, task: Task) -> str:
        """将临时文件原子提交为最终文件，处理重名冲突。返回 'committed' 或 'skipped'。"""
        async with self._commit_lock:
            temp = Path(task.temp_path)
            base = temp.parent
            filename = Path(task.final_path).name
            policy = str(self.config.get("conflict_policy", "rename"))
            final = safe_join(base, filename)
            if final.exists():
                if policy == "skip":
                    return "skipped"
                if policy == "rename":
                    final = self._unique_path(base, filename)
                # overwrite：保持原路径，os.replace 直接覆盖
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, final)
            task.final_path = str(final)
            return "committed"

    def _prepare_paths(self, task: Task) -> None:
        base = Path(task.save_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        classify = str(self.config.get("classify_by", "none"))
        if classify == "type":
            base = base / (guess_category(task.filetype) or "其他")
        elif classify == "domain":
            base = base / (_domain_of(task.url) or "unknown")
        elif classify == "batch" and task.batch_id:
            base = base / sanitize_filename(task.batch_id, "batch")
        base.mkdir(parents=True, exist_ok=True)

        task.temp_path = str(base / f".task{task.id}.part")
        self._resolve_final_path(task)

    def _resolve_final_path(self, task: Task) -> None:
        base = Path(task.temp_path).parent
        filename = task.filename or "download"
        policy = str(self.config.get("conflict_policy", "rename"))
        candidate = safe_join(base, filename)
        if candidate.exists() and policy == "rename":
            candidate = self._unique_path(base, filename)
        task.final_path = str(candidate)

    @staticmethod
    def _unique_path(base: Path, filename: str) -> Path:
        stem = Path(filename).stem
        ext = Path(filename).suffix
        i = 1
        while True:
            cand = base / f"{stem} ({i}){ext}"
            if not cand.exists():
                return cand
            i += 1

    @staticmethod
    def _remove_temp(task: Task) -> None:
        if task.temp_path:
            try:
                p = Path(task.temp_path)
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    async def _finish_failed(self, task: Task, error: str) -> None:
        task.status = Status.FAILED
        task.error = error
        task.completed_at = now_iso()
        # 保留 .part 以便后续续传（仅当确有部分数据）
        try:
            if task.temp_path and Path(task.temp_path).stat().st_size == 0:
                self._remove_temp(task)
        except OSError:
            pass
        await self._finish_terminal(task)

    async def _finish_terminal(self, task: Task) -> None:
        task.updated_at = now_iso()
        task.speed = 0.0
        task.eta = 0.0
        await self.db.save(task)
        await self._emit_task(task)

    # ---------- 打开文件/目录 ----------

    def open_path(self, path: str, is_dir: bool = False) -> None:
        """在系统资源管理器中打开文件或目录（仅限授权下载目录内）。"""
        allowed_roots = self._allowed_roots()
        if not any(path_is_under(r, path) for r in allowed_roots):
            raise SecurityError("只能打开下载目录内的文件/文件夹")
        p = Path(path)
        if is_dir or p.is_dir():
            if not p.is_dir():
                raise SecurityError("目录不存在")
            self._open_in_explorer(str(p))
        else:
            if not p.exists():
                raise SecurityError("文件不存在")
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                self._open_in_explorer(str(p.parent))

    @staticmethod
    def _open_in_explorer(path: str) -> None:
        if os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])

    def _allowed_roots(self) -> list[Path]:
        roots = {self.config.resolved_download_dir(self.project_root)}
        for t in self.tasks.values():
            if t.save_dir:
                roots.add(Path(t.save_dir).expanduser().resolve())
        return [r.resolve() for r in roots if str(r)]


def _parse_content_length(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_content_range_total(value: str) -> int:
    # 形如 "bytes 0-1023/4096"
    if "/" not in value:
        return 0
    total = value.rsplit("/", 1)[-1].strip()
    if total == "*":
        return 0
    try:
        return int(total)
    except ValueError:
        return 0


def _http_reason(resp: httpx.Response) -> str:
    try:
        return resp.reason_phrase or ""
    except Exception:  # noqa: BLE001
        return ""
