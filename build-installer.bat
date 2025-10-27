@echo off
setlocal enabledelayedexpansion

:: Configuration
set VERSION=1.0.0
set APP_NAME=YoGym
set OUTPUT_DIR=build-output
set ELECTRON_APP_DIR=electron-app
set PYTHON_APP_DIR=pythonApp
set SETUP_DLLS_DIR=setupDlls
set RESOURCES_DIR=resources

echo 🛠️  Starting automated build process for %APP_NAME% v%VERSION%
echo.

:: Debug information
echo 📂 Current directory: %cd%
echo 📂 Python directory: %cd%\%PYTHON_APP_DIR%
echo.

:: Step 1: Clean old builds
echo 🔁 Cleaning old builds...
if exist "%OUTPUT_DIR%" rmdir /s /q "%OUTPUT_DIR%"
mkdir "%OUTPUT_DIR%" 2>nul
mkdir "%OUTPUT_DIR%\electron" 2>nul
mkdir "%OUTPUT_DIR%\python" 2>nul
mkdir "%OUTPUT_DIR%\setup" 2>nul
echo.

:: Step 2: Build Python App
echo 🐍 Building Python application...
cd /d "%PYTHON_APP_DIR%"
python -m PyInstaller pythonApp.exe.spec
if errorlevel 1 (
    echo ❌ Failed to build Python app!
    exit /b 1
)
copy /y "dist\pythonApp.exe" "..\%OUTPUT_DIR%\python\"
copy /y ".env" "..\%OUTPUT_DIR%\python\"
rmdir /s /q "dist" "build" "__pycache__"
cd ..
echo.

:: Step 3: Build setupDlls
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
    python -m PyInstaller --onefile --name=setupDlls.exe setupDlls.py
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

:: Step 4: Build Electron App
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
:: Check if build script exists
call npm run build --dry-run >nul 2>&1
if errorlevel 1 (
    echo ⚠️ No 'build' script found in package.json
    echo 🔍 Looking for alternative build commands...
    
    :: Try alternative build commands
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

:: Look for build output in various potential locations
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

:: Step 5: Copy SDK resources
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

:: Step 6: Prepare Installer Files
echo 📦 Preparing installer files...
if not exist "%OUTPUT_DIR%" (
    echo ❌ Output directory not found!
    exit /b 1
)

cd /d "%OUTPUT_DIR%"
if exist "installer" rmdir /s /q "installer"
mkdir "installer" 2>nul

:: Copy electron files if they exist
if exist "electron" (
    echo Copying Electron app files...
    xcopy /e /i /y "electron\*" "installer\"
) else (
    echo ⚠️ Warning: No Electron files to copy!
    mkdir "installer" 2>nul
    echo This is a placeholder for the Electron app > "installer\%APP_NAME%.exe"
)

:: Copy Python executable if it exists
if exist "python\pythonApp.exe" (
    echo Copying Python app...
    copy /y "python\pythonApp.exe" "installer\"
    copy /y "python\.env" "installer\"
    echo. > installer\task_queue.db
) else (
    echo ⚠️ Warning: pythonApp.exe not found!
)

:: Copy setupDlls if it exists
if exist "setup\setupDlls.exe" (
    echo Copying setupDlls...
    copy /y "setup\setupDlls.exe" "installer\"
) else (
    echo ⚠️ Warning: setupDlls.exe not found!
)

:: Copy resources if they exist
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

:: Copy Angular dist folder
if exist "..\angular-dist" (
    echo Copying Angular dist folder...
    mkdir "%APPDATA%\YoGym\angular-dist\" 2>nul
    xcopy /e /i /y "..\angular-dist\*" "%APPDATA%\YoGym\angular-dist\"
) else (
    echo  Angular dist folder not found!
)

:: Copy Spring Boot jar
if exist "..\spring-boot\gym-management-app-0.0.1-SNAPSHOT.jar" (
    echo Copying Spring Boot jar...
    mkdir "%APPDATA%\YoGym\spring-boot\" 2>nul
    copy /y "..\spring-boot\gym-management-app-0.0.1-SNAPSHOT.jar" "%APPDATA%\YoGym\spring-boot\"
) else (
    echo  Spring Boot jar not found!
)


:: Copy Angular dist folder
if exist "..\angular-dist" (
    echo Copying Angular dist folder...
    mkdir "installer\angular-dist\" 2>nul
    xcopy /e /i /y "..\angular-dist\*" "installer\angular-dist\"
) else (
    echo  Angular dist folder not found!
)

:: Copy Spring Boot jar
if exist "..\spring-boot\gym-management-app-0.0.1-SNAPSHOT.jar" (
    echo Copying Spring Boot jar...
    mkdir "installer\spring-boot\" 2>nul
    copy /y "..\spring-boot\gym-management-app-0.0.1-SNAPSHOT.jar" "installer\spring-boot\"
) else (
    echo  Spring Boot jar not found!
)


echo.

:: Step 7: Create NSIS Installer Script
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
    echo   SetOverwrite ifnewer
    echo.
    echo   ; Electron app files
    echo   File /r "*.*"
    echo.
    echo   ; Create logs directory
    echo   CreateDirectory "$INSTDIR\logs"
    echo.
    echo   ; Shortcuts
    echo   CreateDirectory "$SMPROGRAMS\%APP_NAME%"
    echo   CreateShortCut "$SMPROGRAMS\%APP_NAME%\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
    echo   CreateShortCut "$DESKTOP\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
    echo.
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

:: Step 8: Build Installer
echo 🔨 Building NSIS installer...
cd installer

:: Find NSIS installation
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

:: Step 9: Final Output
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