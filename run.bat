@echo off
ECHO Checking for Python...
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    ECHO Python not found. Please install Python 3.7+ from python.org and try again.
    pause
    exit /b 1
)

ECHO Setting up virtual environment...
IF NOT EXIST venv (
    python -m venv venv
    ECHO Virtual environment created.
)

ECHO Activating virtual environment...
call venv\Scripts\activate

ECHO Installing dependencies from requirements.txt...
pip install -r requirements.txt

ECHO Starting osu-ahr-py...
python osu-ahr.py

pause
