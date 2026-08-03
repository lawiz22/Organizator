@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ==============================================
echo   DJI ORGANIZATOR - Classement media drones
echo ==============================================
echo.

:: ---- Téléchargement de UV si absent ----
if not exist ".uv\uv.exe" (
    echo [1/5] Telechargement du gestionnaire de paquets UV...
    mkdir .uv 2>nul
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '.uv\uv.zip' -UseBasicParsing"
    if not exist ".uv\uv.zip" (
        echo [ERREUR] Impossible de telecharger UV.
        pause & exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path '.uv\uv.zip' -DestinationPath '.uv' -Force"
    del ".uv\uv.zip" 2>nul
)

:: ---- Création du .venv si absent ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/5] Creation de l'environnement Python 3.13...
    .uv\uv.exe venv .venv --python 3.13
    if errorlevel 1 (
        echo [ERREUR] Echec de creation du .venv.
        pause & exit /b 1
    )
)

:: ---- Installation des dépendances DJI Organizator ----
:: Contrôle robuste : on ré-installe systématiquement à partir de
:: requirements_dji.txt si un des paquets critiques manque. Un simple
:: test de dossier par paquet ne suffit pas (l'install peut avoir echoue
:: silencieusement avant, laissant .venv incomplet).
set _NEED_INSTALL=0
for %%P in (nicegui webview exiftool PIL cv2 send2trash) do (
    .venv\Scripts\python.exe -c "import %%P" 1>nul 2>nul
    if errorlevel 1 (
        set _NEED_INSTALL=1
    )
)

if "!_NEED_INSTALL!"=="1" (
    echo [3/5] Installation des dependances DJI Organizator...
    echo       (nicegui, pywebview, PyExifTool, Pillow, opencv-python, send2trash)
    .uv\uv.exe pip install -r requirements_dji.txt
    if errorlevel 1 (
        echo [ERREUR] Echec install des dependances DJI.
        echo Verifiez votre connexion et relancez.
        pause & exit /b 1
    )

    :: Vérification post-install : chaque paquet critique doit être importable
    for %%P in (nicegui webview exiftool PIL cv2 send2trash) do (
        .venv\Scripts\python.exe -c "import %%P" 1>nul 2>nul
        if errorlevel 1 (
            echo [ERREUR] Le paquet %%P n'a pas ete installe correctement.
            echo Essayez : .venv\Scripts\python.exe -m pip install -r requirements_dji.txt
            pause & exit /b 1
        )
    )
) else (
    echo [3/5] Dependances Python OK.
)

:: ---- Téléchargement d'ExifTool binaire si absent ----
:: Utilise le paquet Oliver Betz (launcher .exe + dossier exiftool_files avec Perl embarqué)
if not exist ".tools\exiftool\exiftool.exe" (
    echo [4/5] Telechargement d'ExifTool...
    mkdir ".tools\exiftool" 2>nul
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://exiftool.org/exiftool-13.00_64.zip' -OutFile '.tools\exiftool\exiftool.zip' -UseBasicParsing"
    if not exist ".tools\exiftool\exiftool.zip" (
        echo [ERREUR] Echec du telechargement d'ExifTool.
        echo Telechargez manuellement depuis https://exiftool.org/ et placez exiftool.exe dans .tools\exiftool\
        pause & exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path '.tools\exiftool\exiftool.zip' -DestinationPath '.tools\exiftool' -Force"
    del ".tools\exiftool\exiftool.zip" 2>nul

    :: Cas 1 (paquet Oliver Betz récent) : le launcher exiftool.exe est déjà présent
    :: mais le dossier des dépendances Perl s'appelle 'exiftool' — il DOIT être renommé
    :: en 'exiftool_files' sinon le launcher ne trouve pas perl5*.dll.
    if exist ".tools\exiftool\exiftool" (
        if not exist ".tools\exiftool\exiftool_files" (
            ren ".tools\exiftool\exiftool" "exiftool_files"
        )
    )

    :: Cas 2 (ancien paquet Phil Harvey) : exécutable nommé exiftool(-k).exe
    if exist ".tools\exiftool\exiftool(-k).exe" (
        move /Y ".tools\exiftool\exiftool(-k).exe" ".tools\exiftool\exiftool.exe" >nul
    )

    :: Cas 3 (paquet dans un sous-dossier exiftool-*)
    for /d %%D in (".tools\exiftool\exiftool-*") do (
        if exist "%%D\exiftool(-k).exe" (
            move /Y "%%D\exiftool(-k).exe" ".tools\exiftool\exiftool.exe" >nul
        )
        if exist "%%D\exiftool_files" (
            xcopy /E /I /Y "%%D\exiftool_files" ".tools\exiftool\exiftool_files" >nul
        )
    )
) else (
    echo [4/5] ExifTool OK.
)

:: ---- Fermer une ancienne instance (port 8192) ----
echo [5/5] Verification du port 8192...
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8192 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){Write-Host ('Fermeture de l''ancienne instance (PID(s): ' + ($p -join ', ') + ')...'); foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

:: ---- Vérification finale du port ----
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8192 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

echo.
echo Demarrage de DJI Organizator...
echo.

.venv\Scripts\python.exe dji_organizator.py %*

pause
