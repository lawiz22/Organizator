@echo off
:: Lanceur rapide DJI Organizator (aucune verification d'install).
:: Utilise run_dji_organizator.bat pour la premiere installation ou en cas d'erreur.
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERREUR] .venv introuvable. Lancez d'abord run_dji_organizator.bat pour l'installation.
    pause
    exit /b 1
)

:: Fermer une ancienne instance sur le port 8192 si presente
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8192 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

.venv\Scripts\python.exe dji_organizator.py %*
