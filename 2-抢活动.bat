@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _py.bat grab
echo.
pause
