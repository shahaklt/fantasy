@echo off
REM Update Gridiron in place. Your downloaded NFL data, league settings and
REM credentials live in gitignored directories and are never touched.
setlocal
cd /d "%~dp0"

git diff --quiet
if errorlevel 1 (
  echo ==^> You have uncommitted changes. Commit or stash them first.
  git status --short
  exit /b 1
)

for /f %%i in ('git rev-parse HEAD') do set BEFORE=%%i
for /f %%i in ('git hash-object pyproject.toml') do set DEPS_BEFORE=%%i
for /f %%i in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%i

echo ==^> Fetching origin/%BRANCH%
git fetch origin %BRANCH%
git merge --ff-only origin/%BRANCH%
if errorlevel 1 (
  echo ==^> Cannot fast-forward. Resolve with: git pull --rebase origin %BRANCH%
  exit /b 1
)

for /f %%i in ('git rev-parse HEAD') do set AFTER=%%i
if "%BEFORE%"=="%AFTER%" (
  echo ==^> Already up to date
) else (
  echo ==^> Updated. New commits:
  git log --oneline %BEFORE%..%AFTER%
)

if not exist ".venv" (
  echo ==^> No virtualenv found - run run.bat for first-time setup.
  exit /b 1
)
call .venv\Scripts\activate.bat

for /f %%i in ('git hash-object pyproject.toml') do set DEPS_AFTER=%%i
if not "%DEPS_BEFORE%"=="%DEPS_AFTER%" (
  echo ==^> Dependencies changed - syncing
  python -m pip install --upgrade-strategy only-if-needed -e ".[dev]"
) else (
  echo ==^> Dependencies unchanged - skipping pip
)

if not "%BEFORE%"=="%AFTER%" (
  echo.
  echo ==^> Rebuild simulations to pick up model changes:
  echo     gridiron build --sims 20000
)
pause
