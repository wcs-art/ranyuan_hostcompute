@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  EV1 上位机启动脚本（源码方式，不依赖安装包）
REM
REM  用法一（最省事）：改 config\run.json 里的 source/proto/port，然后双击本文件
REM  用法二（临时换参数）：命令行执行
REM        run.bat --source=real --proto=udp --port=5000 --raw-dump=capture\field.bin
REM
REM  优先级：命令行 > config\run.json
REM ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv\Scripts\python.exe
    echo        本脚本必须在项目根目录运行，且虚拟环境需已创建。
    pause
    exit /b 1
)

echo [启动] 参数来自 config\run.json，命令行可覆盖
".venv\Scripts\python.exe" main.py %*

set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" (
    echo.
    echo [异常退出] 返回码 %CODE%，查看 logs\run_*.log
    pause
)
endlocal
