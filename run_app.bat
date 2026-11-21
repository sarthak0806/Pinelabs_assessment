@echo off
REM Launch PayInsight Analytics using the project's own virtual environment.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if not exist "data\payinsight.db" ".venv\Scripts\python.exe" data\seed_data.py
".venv\Scripts\python.exe" -m streamlit run app.py
