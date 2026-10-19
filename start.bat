@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [初始化] 首次运行，正在创建虚拟环境并安装依赖，请稍候...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [错误] 未找到可用的 Python，请先安装 Python 3.10 或更高版本。
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo 正在启动本地批量下载器，浏览器将自动打开 http://127.0.0.1:8000
echo 按 Ctrl+C 或关闭本窗口可停止服务（也可双击 stop.bat）。
.venv\Scripts\python.exe run.py
