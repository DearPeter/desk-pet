@echo off
rem ASCII-only launcher for desk_pet.py (avoids codepage issues)
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw desk_pet.py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        start "" /b python desk_pet.py
    ) else (
        echo Python not found. Please install Python 3.9+ and add it to PATH.
        pause
    )
)
