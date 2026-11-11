@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 把媒体文件夹直接拖到这个 bat 上，就会以该文件夹启动并扫描。
rem 也可以双击启动，然后在网页里点「目录」添加。

set "MEDIA_ROOT=%~1"
if defined MEDIA_ROOT (
    python shelf.py --root "%MEDIA_ROOT%"
) else (
    python shelf.py
)

if errorlevel 1 (
    echo.
    echo 启动失败：请确认已安装 Python 3.9 或更高版本，并且可以在命令行运行 python。
    echo 也可以试试：py shelf.py
)
echo.
pause
