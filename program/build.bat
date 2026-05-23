@echo off
echo ========================================
echo  card_reader.exe build script
echo ========================================
echo.

pip show pyinstaller > nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

pip show python-dotenv > nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing python-dotenv...
    pip install python-dotenv
)

pip show nfcpy > nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing nfcpy...
    pip install nfcpy
)

pip show requests > nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing requests...
    pip install requests
)

echo.
echo [BUILD] pyinstaller card_reader.spec
echo.

pyinstaller card_reader.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Check the log above.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build complete!
echo  Output: dist\card_reader\card_reader.exe
echo ========================================
echo.
echo [NOTE] Check dist\card_reader\ contains:
echo   - .env
echo   - student_map.json
echo   - WAV files
echo.
pause