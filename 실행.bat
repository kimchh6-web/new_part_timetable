@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" http://127.0.0.1:5190/
python -m http.server 5190 --bind 127.0.0.1
