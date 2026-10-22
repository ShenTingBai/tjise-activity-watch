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
echo     打不开界面时看同目录的 启动不了点我.txt。
exit /b 1

:check_dep
%PY% -c "import playwright" >nul 2>&1
if not errorlevel 1 goto :run

echo [X] 依赖没装好（缺 playwright）。请先双击 install.bat。
echo     打不开界面时看同目录的 启动不了点我.txt。
exit /b 1

:run
rem 第一个参数是 gui 时启动图形界面：用 pythonw 避免多留一个黑窗口
if /i "%~1"=="gui" goto :rungui
%PY% "%~dp0grab.py" %*
exit /b %errorlevel%

:rungui
rem 界面是 pywebview + WebView2；缺依赖时用 pythonw 启动只会"双击没反应"，所以先查
%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
  echo [X] 界面缺少 pywebview 依赖。请先双击 install.bat 补装。
  pause
  exit /b 1
)
set "GUIW="
where pythonw >nul 2>&1
if not errorlevel 1 set "GUIW=pythonw"
if not defined GUIW set "GUIW=%PY%"
start "" %GUIW% "%~dp0app.py"
exit /b 0
