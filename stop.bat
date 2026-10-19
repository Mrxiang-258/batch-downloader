@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".server.pid" (
    echo 未找到运行中的服务器（缺少 .server.pid 文件）。
    exit /b 1
)

set /p PID=<.server.pid
echo 正在停止服务器（PID %PID%）...
taskkill /PID %PID% /T /F >nul 2>nul
del .server.pid 2>nul
echo 已停止。
