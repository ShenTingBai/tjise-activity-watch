@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   校园活动自动报名（本机版）- 一键安装
echo   只需要双击一次；以后用 1-登录 / 2-抢活动 / 3-检查
echo ============================================================
echo.

set "PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :found

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :found

echo [X] 没有找到 Python 3.10 或更高版本。
echo.
echo     处理办法：
echo       1) 打开 https://www.python.org/downloads/ 下载安装
echo       2) 安装第一屏务必勾选 "Add python.exe to PATH"
echo       3) 关掉这个窗口，重新双击 install.bat
echo.
pause
exit /b 1

:found
echo [i] 使用的 Python：%PY%
%PY% --version
echo.

echo [1/4] 安装 Python 依赖（playwright）...
%PY% -m pip install -r requirements.txt
if not errorlevel 1 goto :deps_ok
echo [!] 默认源失败，改用清华镜像重试...
%PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto :deps_ok
echo [X] 依赖安装失败。把上面的报错截图给人看，或手动执行：
echo     %PY% -m pip install playwright
pause
exit /b 1

:deps_ok
echo [2/4] 下载浏览器内核（约 150 MB，只用一次）...
%PY% -m playwright install chromium
if not errorlevel 1 goto :browser_ok
echo [!] 官方源失败，改用国内镜像重试...
set "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/"
%PY% -m playwright install chromium
if not errorlevel 1 goto :browser_ok
echo [!] 内核没装上。如果你电脑装了 Chrome，脚本会自动回退用 Chrome，可以继续。
echo     装 Chrome：https://www.google.cn/chrome/

:browser_ok
echo [3/4] 准备配置文件...
if exist config.json goto :cfg_ok
copy config.example.json config.json >nul
echo [i] 已生成 config.json（想收飞书通知就在里面填 webhook，不填也行）

:cfg_ok
echo [4/4] 自检（能拉起浏览器 + 能连上学校站点）...
echo.
%PY% grab.py selftest
if errorlevel 1 goto :selftest_bad
echo.
echo ============================================================
echo   安装完成。下一步：双击 1-登录.bat，在弹出的浏览器里登录一次。
echo ============================================================
pause
exit /b 0

:selftest_bad
echo.
echo [X] 自检没通过，先别抢活动。常见原因：
echo     - 网络/代理挡住了 xg.tjise.edu.cn
echo     - 浏览器内核没装好（重跑一次 install.bat）
pause
exit /b 1
