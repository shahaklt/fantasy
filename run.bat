@echo off
REM Start Gridiron locally on Windows. Creates a virtualenv on first run.
cd /d "%~dp0"

if not exist ".venv" (
  echo ==^> Creating virtualenv
  python -m venv .venv
)
call .venv\Scripts\activate.bat

python -c "import gridiron" 2>NUL
if errorlevel 1 (
  echo ==^> Installing dependencies
  python -m pip install --upgrade pip
  python -m pip install -e ".[dev]"
)

python -c "import cupy" 2>NUL
if errorlevel 1 (
  where nvidia-smi >NUL 2>&1
  if not errorlevel 1 (
    echo ==^> NVIDIA GPU detected; installing CuPy
    python -m pip install cupy-cuda12x
  )
)

python -m gridiron.cli serve --host 127.0.0.1 --port 8000
pause
