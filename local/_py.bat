@echo off
rem 供 1-登录 / 2-抢活动 / 3-检查 调用：找 Python、确认依赖装过，再执行 grab.py
setlocal
set "PY="

py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :check_dep

python --version >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :check_dep

echo [X] 没有找到 Python。请先双击 install.bat 完成安装。
exit /b 1

:check_dep
%PY% -c "import playwright" >nul 2>&1
if not errorlevel 1 goto :run

echo [X] 依赖没装好（缺 playwright）。请先双击 install.bat。
exit /b 1

:run
%PY% "%~dp0grab.py" %*
