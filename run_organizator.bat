@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==============================================
echo   ORGANIZATOR - Organisateur de disques durs
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

:: ---- Installation des dépendances de base ----
if not exist ".venv\Lib\site-packages\nicegui" (
    echo Installation des dépendances MediaMind AI...
    .uv\uv.exe pip install -r requirements.txt --index-strategy unsafe-best-match
)

:: ---- Installation des dépendances Organizator ----
if not exist ".venv\Lib\site-packages\asyncpg" (
    echo Installation des dépendances Organizator...
    .uv\uv.exe pip install -r requirements_organizator.txt
)

:: ---- Vérifier support Corbeille ----
if not exist ".venv\Lib\site-packages\send2trash" (
    echo Installation du support Corbeille...
    .uv\uv.exe pip install send2trash
)

:: ---- Création du fichier .env si absent ----
if not exist ".env" (
    echo Création du fichier de configuration .env...
    echo HF_TOKEN=your_huggingface_token_here> .env
    echo.
    echo [ATTENTION] Editez le fichier .env et ajoutez votre token HuggingFace.
)

echo.
echo Démarrage d'Organizator...
echo Astuce: utilisez "run_organizator.bat llm" pour ouvrir directement le mode LLM.
echo Astuce: utilisez "run_organizator.bat llmapi" pour mode LLM avec endpoint API MediaMind preset.
echo.

if "%MEDIAMIND_API_URL%"=="" (
    set "MEDIAMIND_API_URL=http://127.0.0.1:8190/api/llm/enrich"
)

:: ---- Fermer une ancienne instance (port 8191) ----
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8191 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){Write-Host ('Fermeture de l''ancienne instance (PID(s): ' + ($p -join ', ') + ')...'); foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

:: ---- Lancement de l'application ----
:: ---- Vérification finale du port (évite les conflits de relance rapides) ----
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort 8191 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if($p){foreach($id in $p){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}"

set "APP_ARGS=%*"
if /I "%~1"=="llm" (
    shift
    set "APP_ARGS=--llm-mode %*"
)
if /I "%~1"=="llmapi" (
    shift
    set "MEDIAMIND_API_URL=http://127.0.0.1:8190/api/llm/enrich"
    set "APP_ARGS=--llm-mode %*"
)

.venv\Scripts\python.exe organizator.py %APP_ARGS%

pause
