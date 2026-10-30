@echo off
title Cookie 采集同步工具
cd /d "%~dp0"

rem ---- 必须与 cookie_collector.py 同目录（随项目 scripts 目录一起存放）
if not exist "cookie_collector.py" (
    echo [x] 未找到 cookie_collector.py
    echo     本 bat 必须放在项目的 scripts 目录内、与 cookie_collector.py 同目录使用
    echo     不能单独复制到别处运行
    echo     项目位置: ParsingTool\media-parser\scripts\
    pause
    exit /b 1
)

rem ---- 找 Python: 先用现成的 python 真跑一次, 不行再找 py 启动器
set "PY=python"
python -c "import sys" || (
    set "PY="
    where py && set "PY=py -3"
)
if not defined PY (
    echo [x] 未检测到可用的 Python, 请安装 Python 3.10+ 并勾选 Add Python to PATH
    pause
    exit /b 1
)

rem ---- 首次运行: 建独立虚拟环境
set "VENV=..\data\.venv_collector"
if not exist "%VENV%\Scripts\python.exe" (
    echo [首次运行] 创建独立环境...
    %PY% -m venv "%VENV%"
)

rem ---- 依赖自愈: 缺什么补什么, 已有环境也能修复
"%VENV%\Scripts\python.exe" -c "import playwright, requests, flask, tzdata" || (
    echo [依赖检查] 补全安装中, 约 2-5 分钟...
    "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
    "%VENV%\Scripts\python.exe" -m pip install -q playwright requests flask tzdata
    "%VENV%\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 (
        echo [x] 依赖安装失败, 请检查网络后重新运行本脚本
        pause
        exit /b 1
    )
)
echo [√] 环境就绪, 启动采集器...

rem ---- 运行采集器 (参数原样透传, 支持 -p douyin --push 等)
"%VENV%\Scripts\python.exe" cookie_collector.py %*
echo.
pause
