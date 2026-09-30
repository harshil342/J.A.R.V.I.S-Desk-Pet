@echo off
cd /d "%~dp0"
set MINICPM_DEV=1
set DESKPET_AUTO_OPEN_CHAT=1
"C:\Program Files\nodejs\node.exe" "%~dp0clawd-on-desk\launch.js" > "%~dp0deskpet-win.log" 2>&1
