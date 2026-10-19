"""启动入口：运行 uvicorn 后端，并在桌面窗口中显示 Web 界面。"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import uvicorn
import webview

from app.config import Config
from app.main import app


def _data_root() -> Path:
    # 打包成 exe 后，config.json / data / downloads / logs 落在 exe 同级目录。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _data_root()


def _setup_stdio() -> None:
    """处理控制台 / 窗口两种运行模式下的标准输出。"""
    if sys.platform != "win32":
        return
    if not getattr(sys, "frozen", False):
        # 源码运行（有控制台）：只切换代码页避免中文乱码
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
        return
    # 打包后 windowed 模式：stdout/stderr 为 None，重定向到日志文件避免崩溃
    if sys.stdout is None or sys.stderr is None:
        log_dir = ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            f = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = f
            if sys.stderr is None:
                sys.stderr = f
        except OSError:
            pass


def _show_error(msg: str) -> None:
    """窗口模式下的兜底错误弹窗。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, "批量下载器", 0x10)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    cfg = Config(ROOT / "config.json")
    host = str(cfg.get("host", "127.0.0.1"))
    port = int(cfg.get("port", 8000))
    url = f"http://{host}:{port}"

    # 写入 PID 文件，供外部停止脚本使用
    try:
        (ROOT / ".server.pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    # 后端在后台线程运行，主线程交给窗口事件循环
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # 等待服务器就绪，避免窗口打开时后端还没起来
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    print(f"本地批量下载器已启动：{url}")

    try:
        webview.create_window(
            "本地批量下载器",
            url,
            width=1100,
            height=750,
            min_size=(820, 600),
        )
        webview.start()
    finally:
        # 窗口关闭后停止后端并清理 PID 文件
        server.should_exit = True
        server_thread.join(timeout=5)
        try:
            (ROOT / ".server.pid").unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    _setup_stdio()
    try:
        main()
    except Exception as e:  # noqa: BLE001
        _show_error(f"程序启动失败：{e}\n\n详见 logs/app.log")
        raise
