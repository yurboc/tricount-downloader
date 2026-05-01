@echo off
cd /d "%~dp0"
python main.py --current-month %*
pause
