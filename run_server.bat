@echo off
chcp 65001 >nul

echo Demarrage en mode serveur...
call run.bat --server-only --port 8190 %*