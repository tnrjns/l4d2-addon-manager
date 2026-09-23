@echo off
REM Builds L4D2AddonManager.exe from l4d2_addon_manager.py (pywebview edition)
REM Run this on Windows, in the same folder as l4d2_addon_manager.py

echo Installing build requirements...
pip install --upgrade pyinstaller pywebview
if errorlevel 1 (
    echo.
    echo Failed to install requirements. Make sure Python and pip are installed
    echo and available on PATH, then try again.
    pause
    exit /b 1
)

echo.
echo Building L4D2AddonManager.exe (this can take a minute)...
python -m PyInstaller --onefile --windowed --name "L4D2AddonManager" --collect-all webview --collect-all clr_loader --collect-all pythonnet l4d2_addon_manager.py

if errorlevel 1 (
    echo.
    echo Build failed — scroll up to see the error from PyInstaller.
    echo If it's complaining about a missing webview backend module, try adding
    echo   --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium
    echo to the pyinstaller command above and re-running this script.
    pause
    exit /b 1
)

echo.
echo Done! Your exe is at: dist\L4D2AddonManager.exe
echo You can copy that single file anywhere and run it directly -
echo no Python window, no console, just the app.
pause
