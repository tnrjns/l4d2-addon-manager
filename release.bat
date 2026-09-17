@echo off
REM Tags a new version and pushes it, which triggers the GitHub Actions
REM workflow to build the exe and publish a release automatically.

set /p VERSION="Version to release (e.g. 1.0.0): "

if "%VERSION%"=="" (
    echo No version entered. Aborting.
    pause
    exit /b 1
)

echo.
echo Committing any pending changes...
git add -A
git commit -m "Release v%VERSION%" || echo (nothing new to commit)

echo.
echo Pushing code...
git push
if errorlevel 1 (
    echo Push failed. Check your git setup and try again.
    pause
    exit /b 1
)

echo.
echo Tagging v%VERSION%...
git tag v%VERSION%
git push origin v%VERSION%
if errorlevel 1 (
    echo Tag push failed - does that tag already exist?
    pause
    exit /b 1
)

echo.
echo Done! GitHub Actions is now building the exe.
echo Watch it here: your repo's "Actions" tab.
echo When it finishes, the release will appear under "Releases".
pause
