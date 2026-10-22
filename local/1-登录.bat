@echo off
chcp 65001 >nul
cd /d "%~dp0"
python grab.py login
echo.
pause
