@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined HARNESS_PUBLIC_ORIGIN set "HARNESS_PUBLIC_ORIGIN=https://timetable.shinick.dev"
start "" http://127.0.0.1:5191/#/live
python web_demo.py
