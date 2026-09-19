@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem This window is a launcher only. It hands off to the GUI-subsystem script and
rem exits immediately, so the public server lives in the scheduled tasks and
rem closing this window - or any window - cannot take the site down.
set "LAUNCHER=%~dp0실행.vbs"
if not exist "%LAUNCHER%" goto :missing
start "" wscript.exe "%LAUNCHER%"
exit /b 0

:missing
echo Launcher not found: %LAUNCHER%
echo Run the GUI launcher directly, or see examples\PUBLIC_SERVER.md.
pause
exit /b 1
