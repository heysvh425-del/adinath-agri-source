@echo off
REM One-command setup + launch for Windows.
cd /d "%~dp0backend"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat
echo Installing dependencies (first run only takes a minute)...
pip install -q -r requirements.txt

echo.
python run.py
pause
