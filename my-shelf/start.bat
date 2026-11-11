@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 把媒体文件夹拖到本文件上 = 以该文件夹启动并扫描。
rem 直接双击 = 使用 config.json 里保存的目录（也可在网页「目录」里增删）。
rem 已经有一个片库在跑时，会直接打开页面，不会重复启动。

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo.
    echo [启动失败] 没有找到 Python。
    echo 请安装 Python 3.9 或更高版本，安装时记得勾选 "Add Python to PATH"。
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

set "MEDIA_ROOT=%~1"
if defined MEDIA_ROOT (
    %PY% shelf.py --root "%MEDIA_ROOT%"
) else (
    %PY% shelf.py
)

if errorlevel 1 (
    echo.
    echo [服务已退出] 若上面报的是端口或路径错误，可改用：
    echo     %PY% shelf.py --port 9000
    echo 或在网页里点「目录」重新配置媒体文件夹。
)
echo.
pause
