@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: Se placer dans le dossier du script
cd /d "%~dp0"

:: Dossiers UV locaux
set "UV_CACHE_DIR=%~dp0.uv_cache"
set "UV_PYTHON_INSTALL_DIR=%~dp0.uv_python"

:: ==============================================================
:: ETAPE 1: Premiere installation (venv + dependances)
:: ==============================================================
if not exist ".venv\Scripts\python.exe" (
    echo ==========================================
    echo Premiere execution: initialisation en cours...
    echo ==========================================

    if not exist ".bin\uv.exe" (
        echo [1/5] Telechargement de uv...
        mkdir .bin 2>nul
        powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '.bin\uv.zip'"
        powershell -NoProfile -Command "Expand-Archive -Path '.bin\uv.zip' -DestinationPath '.bin' -Force"
        move /y ".bin\uv-x86_64-pc-windows-msvc\uv.exe" ".bin\uv.exe" >nul
        rmdir /s /q ".bin\uv-x86_64-pc-windows-msvc"
        del /q ".bin\uv.zip"
    )

    echo [2/5] Creation de l'environnement Python 3.13...
    ".bin\uv.exe" venv --python 3.13 .venv

    echo [3/5] Installation des dependances...
    ".bin\uv.exe" pip install -r requirements.txt --index-strategy unsafe-best-match
    copy /y requirements.txt ".venv\requirements.installed" >nul

    echo[4/5] Nettoyage du cache uv...
    ".bin\uv.exe" cache clean

    echo[5/5] Generation du fichier .env...
    if not exist ".env" (
        echo # Configuration AI Media Organizer Pro > .env
        echo # HF_TOKEN=hf_votre_token_ici >> .env
        echo PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True >> .env
        echo. >> .env
        echo # Si SageAttention requiert CUDA Toolkit 13, indiquez le chemin ci-dessous ^(optionnel^) >> .env
        echo # Exemple: CUSTOM_CUDA_PATH=C:\cuda_13.0 >> .env
        echo # CUSTOM_CUDA_PATH= >> .env
        echo.
        echo [ATTENTION] Le fichier .env vient d'etre cree.
        echo Ajoutez HF_TOKEN dans .env, puis relancez le script.
        notepad .env
        exit /b 0
    )
    echo ==========================================
    echo Installation initiale terminee !
    echo ==========================================
)

:: ==============================================================
:: ETAPE 2: Verifier les mises a jour de dependances
:: ==============================================================

:: Reinstaller si requirements.txt a change
if exist "requirements.txt" (
    if exist ".venv\requirements.installed" (
        fc requirements.txt ".venv\requirements.installed" >nul 2>nul
        if errorlevel 1 (
            echo [INFO] requirements.txt a change. Mise a jour des dependances...
            ".bin\uv.exe" pip install -r requirements.txt --index-strategy unsafe-best-match
            copy /y requirements.txt ".venv\requirements.installed" >nul
            ".bin\uv.exe" cache clean
        )
    ) else (
        echo [INFO] Aucune trace d'installation precedente. Installation des dependances...
        ".bin\uv.exe" pip install -r requirements.txt --index-strategy unsafe-best-match
        copy /y requirements.txt ".venv\requirements.installed" >nul
    )
)

echo Demarrage AI Media Organizer Pro...

:: Charger les variables du fichier .env
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if not "%%b"=="" set "%%a=%%b"
    )
)

:: Configuration CUDA personnalisee (optionnelle)
if defined CUSTOM_CUDA_PATH (
    echo [INFO] Utilisation du chemin CUDA personnalise: !CUSTOM_CUDA_PATH!
    set "CUDA_HOME=!CUSTOM_CUDA_PATH!"
    set "CUDA_PATH=!CUSTOM_CUDA_PATH!"
    :: Ajouter bin au PATH (nvcc et DLL CUDA)
    set "PATH=!CUSTOM_CUDA_PATH!\bin;!PATH!"
)

:: Lancer l'application
".venv\Scripts\python.exe" media_mind_ai.py %*

pause