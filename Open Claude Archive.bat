@echo off
rem Double-click to open your Claude export in the browser.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 viewer.py %*) || (python viewer.py %*)
pause
