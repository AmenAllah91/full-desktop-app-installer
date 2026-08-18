@echo off
setlocal enabledelayedexpansion

:: =======================
:: Configuration
:: =======================
set VERSION=1.0.0
set APP_NAME=YoGym
set OUTPUT_DIR=build-output
set ELECTRON_APP_DIR=electron-app
set PYTHON_APP_DIR=pythonApp
set SETUP_DLLS_DIR=setupDlls
set RESOURCES_DIR=resources
set VENV_PY=%~dp0%PYTHON_APP_DIR%\venv\Scripts\python.exe

:: =======================
:: Cible de deploiement — PRODUCTION par defaut
:: =======================
:: Le .env livre etait auparavant celui du poste de developpement : il n'etait
:: genere que "if not exist", puis copie tel quel dans l'installeur. Un poste
:: configure sur l'integration produisait donc un installeur qui expediait les
:: pointages des clients vers l'environnement de test, ou ils etaient rejetes
:: faute d'adherents correspondants. La cible est desormais explicite et le
:: fichier livre est toujours regenere.
::
::   build-installer.bat        -> PRODUCTION (defaut)
::   build-installer.bat int    -> INTEGRATION (tests internes, choix delibere)
set TARGET=%~1
if "%TARGET%"=="" set TARGET=prod

if /i "%TARGET%"=="prod" (
    set TARGET_LABEL=PRODUCTION
    set ENV_BASE_URL=https://app.yogym.co
    set ENV_KAFKA_BROKER=51.178.55.238:9094
) else if /i "%TARGET%"=="int" (
    set TARGET_LABEL=INTEGRATION - tests internes
    set ENV_BASE_URL=https://integration.yogym.co
    set ENV_KAFKA_BROKER=54.38.35.221:9094
) else (
    echo ❌ Cible inconnue : "%TARGET%"
    echo    Valeurs acceptees : prod ^(defaut^) ou int
    exit /b 1
)

echo 🛠️  Starting automated build process for %APP_NAME% v%VERSION%
echo.
echo ==========================================================
echo   CIBLE : %TARGET_LABEL%
echo   YOGYM_BASE_URL = %ENV_BASE_URL%
echo   KAFKA_BROKER   = %ENV_KAFKA_BROKER%
echo ==========================================================
echo.
echo 📂 Current directory: %cd%
echo 📂 Python directory: %cd%\%PYTHON_APP_DIR%
echo.

:: =======================
:: Step 1: Clean old builds
:: =======================
echo 🔁 Cleaning old builds...
if exist "%OUTPUT_DIR%" rmdir /s /q "%OUTPUT_DIR%"
mkdir "%OUTPUT_DIR%" 2>nul
mkdir "%OUTPUT_DIR%\electron" 2>nul
mkdir "%OUTPUT_DIR%\python" 2>nul
mkdir "%OUTPUT_DIR%\setup" 2>nul
echo.

:: =======================
:: Step 2: Build Python App
:: =======================
echo 🐍 Building Python application...
cd /d "%PYTHON_APP_DIR%"

:: Create virtual environment if not exists
if not exist "venv" (
    python -m venv venv
    echo ✅ Virtual environment created.
)

:: Installation des dependances : on appelle le python du venv par son chemin
:: explicite. L'ancien "call venv\Scripts\activate" suivi de "call deactivate"
:: desactivait le venv AVANT l'appel a PyInstaller plus bas, qui tournait donc
:: sur le Python global, sans aucune des dependances de l'application.
if exist "requirements.txt" (
    echo 📦 Installing Python dependencies...
    "%VENV_PY%" -m pip install -r requirements.txt
) else (
    echo ⚠️ requirements.txt not found! Make sure dependencies are installed manually.
)

:: PyInstaller est un outil de build, pas une dependance applicative : on
:: l'installe dans le venv sans l'ajouter a requirements.txt.
echo 📦 Ensuring PyInstaller is available...
"%VENV_PY%" -m pip install pyinstaller

:: Le .env livre n'est PLUS produit ici : il est genere dans le repertoire de
:: staging juste avant l'empaquetage NSIS, a partir de la cible choisie. Le
:: pythonApp\.env du poste sert uniquement au developpement local et ne quitte
:: jamais la machine.






"%VENV_PY%" -m PyInstaller --onedir --noupx --name=pythonApp main.py --hidden-import=Crypto --hidden-import=Crypto.Cipher --hidden-import=Crypto.Hash --hidden-import=Crypto.Random --hidden-import=Crypto.Util --collect-all certifi
if errorlevel 1 (
    echo ❌ Failed to build Python app!
    exit /b 1
)
xcopy /e /i /y "dist\pythonApp" "..\%OUTPUT_DIR%\python\"
copy /y "libzkfpcsharp.dll" "..\%OUTPUT_DIR%\python\"
rmdir /s /q "dist" "build" "__pycache__"
cd ..
echo.

:: =======================
:: Step 3: Build setupDlls
:: =======================
echo ⚙️  Building SDK installer...
if not exist "%SETUP_DLLS_DIR%" (
    echo ❌ Error: setupDlls directory not found!
    echo Current directory: %cd%
    echo Looking for setupDlls.py...

    for /f "delims=" %%i in ('dir /b /s setupDlls.py 2^>nul') do (
        echo 🔍 Found setupDlls.py at: %%i
        set SETUP_DLLS_PATH=%%~dpi
        cd /d "%%~dpi"
        echo Changed directory to: %%~dpi
        goto :build_setup_dlls
    )

    echo ❌ setupDlls.py not found!
    exit /b 1
) else (
    cd /d "%SETUP_DLLS_DIR%"
)

:build_setup_dlls
echo 🔨 Building setupDlls.exe...
if exist "setupDlls.py" (
    "%VENV_PY%" -m PyInstaller --onefile --name=setupDlls.exe setupDlls.py
) else (
    echo ❌ setupDlls.py not found in %cd%!
    dir *.py /b
    exit /b 1
)

if errorlevel 1 (
    echo ❌ Failed to build SDK installer!
    exit /b 1
)

if exist "dist\setupDlls.exe" (
    if not exist "..\%OUTPUT_DIR%\setup" mkdir "..\%OUTPUT_DIR%\setup" 2>nul
    copy /y "dist\setupDlls.exe" "..\%OUTPUT_DIR%\setup\"
    rmdir /s /q "dist" "build" "__pycache__" 2>nul
    if exist "setupDlls.spec" del "setupDlls.spec" 2>nul
) else (
    echo ❌ setupDlls.exe was not built successfully!
    exit /b 1
)

cd ..
echo.

:: =======================
:: Step 4: Build Electron App
:: =======================
echo 🎨 Building Electron application...
if not exist "%ELECTRON_APP_DIR%" (
    echo ❌ Error: electron-app directory not found!
    echo Current directory: %cd%
    dir /b
    exit /b 1
)

cd /d "%ELECTRON_APP_DIR%"
echo 📦 Installing npm dependencies...
call npm install
if errorlevel 1 (
    echo ❌ npm install failed!
    exit /b 1
)

echo 🔍 Checking available npm scripts...
call npm run --silent

echo 🔨 Building Electron app...
call npm run build --dry-run >nul 2>&1
if errorlevel 1 (
    echo ⚠️ No 'build' script found in package.json
    echo 🔍 Looking for alternative build commands...

    call npm run dist --dry-run >nul 2>&1
    if not errorlevel 1 (
        echo 🔨 Running 'npm run dist' instead...
        call npm run dist
    ) else (
        call npm run make --dry-run >nul 2>&1
        if not errorlevel 1 (
            echo 🔨 Running 'npm run make' instead...
            call npm run make
        ) else (
            call npm run package --dry-run >nul 2>&1
            if not errorlevel 1 (
                echo 🔨 Running 'npm run package' instead...
                call npm run package
            ) else (
                call npm run electron:build --dry-run >nul 2>&1
                if not errorlevel 1 (
                    echo 🔨 Running 'npm run electron:build' instead...
                    call npm run electron:build
                ) else (
                    echo ❌ No build script found! Available scripts:
                    call npm run

                    echo.
                    echo 📄 Contents of package.json:
                    type package.json

                    echo.
                    echo ⚠️ Proceeding without building Electron app...
                    mkdir "dist\win-unpacked" 2>nul
                    echo Placeholder > "dist\win-unpacked\README.txt"
                )
            )
        )
    )
) else (
    call npm run build
)

if exist "dist\win-unpacked" (
    echo ✅ Found build output in dist\win-unpacked
    if not exist "..\%OUTPUT_DIR%\electron" mkdir "..\%OUTPUT_DIR%\electron" 2>nul
    xcopy /e /i /y "dist\win-unpacked" "..\%OUTPUT_DIR%\electron"
) else if exist "out\win-unpacked" (
    echo ✅ Found build output in out\win-unpacked
    if not exist "..\%OUTPUT_DIR%\electron" mkdir "..\%OUTPUT_DIR%\electron" 2>nul
    xcopy /e /i /y "out\win-unpacked" "..\%OUTPUT_DIR%\electron"
) else if exist "dist\win" (
    echo ✅ Found build output in dist\win
    if not exist "..\%OUTPUT_DIR%\electron" mkdir "..\%OUTPUT_DIR%\electron" 2>nul
    xcopy /e /i /y "dist\win" "..\%OUTPUT_DIR%\electron"
) else if exist "build" (
    echo ✅ Found build output in build directory
    if not exist "..\%OUTPUT_DIR%\electron" mkdir "..\%OUTPUT_DIR%\electron" 2>nul
    xcopy /e /i /y "build" "..\%OUTPUT_DIR%\electron"
) else (
    echo ⚠️ Electron build directory not found!
    echo 🔍 Looking for any output directories...
    dir dist /b 2>nul
    dir out /b 2>nul
    dir build /b 2>nul

    echo.
    echo 📑 Creating placeholder Electron files...
    if not exist "..\%OUTPUT_DIR%\electron" mkdir "..\%OUTPUT_DIR%\electron" 2>nul
    echo This is a placeholder for the Electron app > "..\%OUTPUT_DIR%\electron\%APP_NAME%.exe"
    echo The Electron build process failed - please check the package.json file > "..\%OUTPUT_DIR%\electron\README.txt"
)

cd ..
echo.

:: =======================
:: Step 5: Copy SDK resources
:: =======================
echo 📦 Copying SDK resources...
if not exist "%RESOURCES_DIR%" (
    echo ⚠️ Warning: resources directory not found!
    echo Searching for SDK.zip...
    for /f "delims=" %%i in ('dir /b /s SDK.zip 2^>nul') do (
        echo 🔍 Found SDK.zip at: %%i
        if not exist "%OUTPUT_DIR%\resources" mkdir "%OUTPUT_DIR%\resources" 2>nul
        copy /y "%%i" "%OUTPUT_DIR%\resources\"
        echo Copied SDK.zip to %OUTPUT_DIR%\resources\
        goto :resources_copied
    )
    echo ⚠️ SDK.zip not found! Continuing without it...
) else (
    if exist "%RESOURCES_DIR%\SDK.zip" (
        if not exist "%OUTPUT_DIR%\resources" mkdir "%OUTPUT_DIR%\resources" 2>nul
        copy /y "%RESOURCES_DIR%\SDK.zip" "%OUTPUT_DIR%\resources\"
    ) else (
        echo ⚠️ Warning: SDK.zip not found in resources directory!
        dir "%RESOURCES_DIR%" /b
    )
)

:resources_copied
echo.

:: =======================
:: Step 6: Prepare Installer Files
:: =======================
echo 📦 Preparing installer files...
if not exist "%OUTPUT_DIR%" (
    echo ❌ Output directory not found!
    exit /b 1
)

cd /d "%OUTPUT_DIR%"
if exist "installer" rmdir /s /q "installer"
mkdir "installer" 2>nul

if exist "electron" (
    echo Copying Electron app files...
    xcopy /e /i /y "electron\*" "installer\"
) else (
    echo ⚠️ Warning: No Electron files to copy!
    mkdir "installer" 2>nul
    echo This is a placeholder for the Electron app > "installer\%APP_NAME%.exe"
)

if exist "python\pythonApp.exe" (
    echo Copying Python app...
    xcopy /e /i /y "python\*" "installer\"
    echo. > installer\task_queue.db
) else (
    echo ⚠️ Warning: pythonApp.exe not found!
)

:: Le .env livre est genere ICI, systematiquement, a partir de la cible choisie
:: en tete de script. Il n'est jamais copie depuis le poste de developpement.
echo 📝 Generating .env for target: %TARGET_LABEL%
(
echo # Genere automatiquement par build-installer.bat - cible : %TARGET_LABEL%
echo # Ne pas editer a la main : toute modification sera ecrasee a la mise a jour.
echo #
echo # Racine de la plateforme : lue par le pont Python ET par Electron.
echo YOGYM_BASE_URL=%ENV_BASE_URL%
echo.
echo KAFKA_BROKER=%ENV_KAFKA_BROKER%
echo KAFKA_GROUP_ID=group_c
echo KAFKA_TOPIC=rt_pointage
echo.
echo # PAS de TENANT ni de GYM_BRANCH_ID ici, deliberement. Ce fichier est
echo # identique sur tous les postes : y laisser une identite ferait demarrer
echo # un poste vikingsgym sous celle d'empiregym. Les deux valeurs sont saisies
echo # a la premiere ouverture de YoGym, poste par poste, et le pont REFUSE de
echo # demarrer si elles manquent.
echo.
echo FLASK_HOST=0.0.0.0
echo FLASK_PORT=9998
echo.
echo PLCOMPRO_URL=plcommpro.dll
) > "installer\.env"

if exist "setup\setupDlls.exe" (
    echo Copying setupDlls...
    copy /y "setup\setupDlls.exe" "installer\"
) else (
    echo ⚠️ Warning: setupDlls.exe not found!
)

    if exist "..\pythonApp\getuserfacephoto" (
        echo Copying getuserfacephoto folder...
:: resource_path() resout vers sys._MEIPASS, soit _internal\ depuis
:: PyInstaller 6 (l'exe reste a la racine, tout le reste part dans
:: _internal). On copie donc aux DEUX emplacements pour rester
:: compatible avec les deux dispositions.
        mkdir "installer\getuserfacephoto" 2>nul
        copy /y "..\pythonApp\getuserfacephoto\*" "installer\getuserfacephoto\"
        mkdir "installer\_internal\getuserfacephoto" 2>nul
        copy /y "..\pythonApp\getuserfacephoto\*" "installer\_internal\getuserfacephoto\"
    ) else (
        echo  getuserfacephoto folder not found!
    )
mkdir "installer\resources" 2>nul
if exist "resources" (
    echo Copying resources...
    xcopy /e /i /y "resources\*" "installer\resources\"
) else if exist "..\%RESOURCES_DIR%" (
    echo Copying resources from project directory...
    xcopy /e /i /y "..\%RESOURCES_DIR%\*" "installer\resources\"
) else (
    echo ⚠️ Warning: Resources directory not found!
    echo This is a placeholder for resources > "installer\resources\README.txt"
)

echo.

:: =======================
:: Step 7: Create NSIS Installer Script
:: =======================
echo 📝 Generating NSIS installer script...
(
    echo ^^!include "MUI2.nsh"
    echo Name "%APP_NAME%"
    echo OutFile "%APP_NAME%Installer.exe"
    echo InstallDir "$PROGRAMFILES\%APP_NAME%"
    echo CRCCheck on
    echo SilentInstall normal
    echo RequestExecutionLevel admin
    echo.
    echo ^^!define MUI_ABORTWARNING
    echo ^^!define MUI_HEADERIMAGE
    echo.
    echo ^^!insertmacro MUI_PAGE_WELCOME
    echo ^^!insertmacro MUI_PAGE_DIRECTORY
    echo ^^!insertmacro MUI_PAGE_INSTFILES
    echo ^^!insertmacro MUI_PAGE_FINISH
    echo.
    echo Section "Main Application"
    echo   SetOutPath "$INSTDIR"
    echo   ; Le .env porte l'environnement cible. SetOverwrite ifnewer compare les
    echo   ; horodatages, et copy preserve celui de la source : un .env perime
    echo   ; pouvait donc survivre a une mise a jour. On le supprime d'abord.
    echo   Delete "$INSTDIR\.env"
    echo   SetOverwrite ifnewer
    echo   File /r "*.*"
    echo   CreateDirectory "$INSTDIR\logs"
    echo   CreateDirectory "$SMPROGRAMS\%APP_NAME%"
    echo   CreateShortCut "$SMPROGRAMS\%APP_NAME%\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
    echo   CreateShortCut "$DESKTOP\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
    echo   WriteUninstaller "$INSTDIR\uninstall.exe"
    echo SectionEnd
    echo.
    echo Section "SDK Dependencies Setup"
    echo   DetailPrint "Setting up SDK dependencies..."
    echo   nsExec::ExecToLog '"$INSTDIR\setupDlls.exe"'
    echo   DetailPrint "SDK dependencies setup completed"
    echo SectionEnd
    echo.
    echo Section "Uninstall"
    echo   Delete "$INSTDIR\*.*"
    echo   RMDir /r "$INSTDIR\resources"
    echo   RMDir "$INSTDIR\logs"
    echo   RMDir /r "$INSTDIR"
    echo   Delete "$SMPROGRAMS\%APP_NAME%\*.*"
    echo   RMDir "$SMPROGRAMS\%APP_NAME%"
    echo   Delete "$DESKTOP\%APP_NAME%.lnk"
    echo   Delete "$INSTDIR\uninstall.exe"
    echo SectionEnd
    echo.
    echo Function .onInstSuccess
    echo   MessageBox MB_OK "Installation completed successfully!"
    echo FunctionEnd
    echo.
    echo ^^!insertmacro MUI_LANGUAGE "English"
) > "installer\installer.nsi"
echo.

:: =======================
:: Step 8: Build Installer (NSIS)
:: =======================
echo 🔨 Building NSIS installer...
cd installer

set NSIS_FOUND=0
if exist "c:\Program Files (x86)\NSIS\makensis.exe" (
    set NSIS_EXE="c:\Program Files (x86)\NSIS\makensis.exe"
    set NSIS_FOUND=1
) else if exist "C:\Program Files\NSIS\makensis.exe" (
    set NSIS_EXE="C:\Program Files\NSIS\makensis.exe"
    set NSIS_FOUND=1
) else (
    echo ⚠️ NSIS not found in standard locations, searching in PATH...
    where makensis >nul 2>&1
    if not errorlevel 1 (
        set NSIS_EXE=makensis
        set NSIS_FOUND=1
    )
)

if %NSIS_FOUND%==1 (
    echo ✅ Found NSIS at: %NSIS_EXE%
    %NSIS_EXE% installer.nsi
    if errorlevel 1 (
        echo ❌ NSIS build failed! Checking installer script for errors...
        type installer.nsi
        exit /b 1
    )
) else (
    echo ❌ NSIS (makensis.exe) not found!
    echo Please install NSIS from https://nsis.sourceforge.io/Download
    echo.
    echo To continue without NSIS, copying files to output directory...
    copy /y * ..
    mkdir ..\resources 2>nul
    xcopy /e /i /y resources\* ..\resources\
    cd ..
    echo ✅ Files copied to %cd%
    exit /b 0
)

if exist "%APP_NAME%Installer.exe" (
    move "%APP_NAME%Installer.exe" ..
)

cd ..
echo.

:: =======================
:: Step 9: Final Output
:: =======================
echo 🎉 Installer created successfully!
echo.
echo 📦 Output Location:
echo %cd%\%APP_NAME%Installer.exe
echo.
echo ✅ Installation will include:
echo - Electron app (%APP_NAME%.exe)
echo - Python backend (pythonApp.exe)
echo - SDK files (resources directory)
echo - SetupDlls.exe for SDK setup
echo - Desktop & Start menu shortcuts
echo.
echo 🚀 Run the installer to complete installation

endlocal
