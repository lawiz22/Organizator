@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==============================================
echo   DJI ORGANIZATOR - Classement media drones
echo ==============================================
echo.

:: ---- Téléchargement de UV si absent ----
if not exist ".uv\uv.exe" (
    echo Téléchargement du gestionnaire de paquets UV...
    mkdir .uv 2>nul
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '.uv\uv.zip'"
    powershell -Command "Expand-Archive -Path '.uv\uv.zip' -DestinationPath '.uv' -Force"
    del ".uv\uv.zip" 2>nul
)

:: ---- Création du .venv si absent ----
if not exist ".venv\Scripts\python.exe" (
    echo Création de l'environnement Python 3.13...
    .uv\uv.exe venv .venv --python 3.13
)

:: ---- Installation des dépendances de base (partagées avec MediaMind) ----
if not exist ".venv\Lib\site-packages\nicegui" (
    echo Installation des dépendances MediaMind AI...
    .uv\uv.exe pip install -r requirements.txt --index-strategy unsafe-best-match
)

:: ---- Installation des dépendances DJI Organizator ----
if not exist ".venv\Lib\site-packages\exiftool" (
    echo Installation des dépendances DJI Organizator...
    .uv\uv.exe pip install -r requirements_dji.txt
)

:: ---- Vérifier support Corbeille ----
if not exist ".venv\Lib\site-packages\send2trash" (
    echo Installation du support Corbeille...
    .uv\uv.exe pip install send2trash
)

:: ---- Téléchargement d'ExifTool binaire si absent ----
if not exist ".tools\exiftool\exiftool.exe" (
    echo Téléchargement d'ExifTool...
    mkdir ".tools\exiftool" 2>nul
    powershell -Command "Invoke-WebRequest -Uri 'https://exiftool.org/exiftool-13.00_64.zip' -OutFile '.tools\exiftool\exiftool.zip' -UseBasicParsing"
    if not exist ".tools\exiftool\exiftool.zip" (
        echo [ERREUR] Echec du telechargement d'ExifTool.
        echo Telechargez manuellement depuis https://exiftool.org/ et placez exiftool.exe dans .tools\exiftool\
        pause
        exit /b 1
    )
    powershell -Command "Expand-Archive -Path '.tools\exiftool\exiftool.zip' -DestinationPath '.tools\exiftool' -Force"
    del ".tools\exiftool\exiftool.zip" 2>nul
    :: L'archive contient un exécutable nommé exiftool(-k).exe ou exiftool.exe selon la version.
    if exist ".tools\exiftool\exiftool(-k).exe" (
        move /Y ".tools\exiftool\exiftool(-k).exe" ".tools\exiftool\exiftool.exe" >nul
    )
    for /d %%D in (".tools\exiftool\exiftool-*") do (
        if exist "%%D\exiftool(-k).exe" (
            move /Y "%%D\exiftool(-k).exe" ".tools\exiftool\exiftool.exe" >nul
        )
        if exist "%%D\exiftool_files" (
            xcopy /E /I /Y "%%D\exiftool_files" ".tools\exiftool\exiftool_files" >nul
        )
    )
)

:: ---- Fermer une ancienne instance (port 8192) ----
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8192 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){Write-Host ('Fermeture de l''ancienne instance (PID(s): ' + ($p -join ', ') + ')...'); foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

:: ---- Vérification finale du port ----
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8192 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

echo.
echo Démarrage de DJI Organizator...
echo.

.venv\Scripts\python.exe dji_organizator.py %*

pause
