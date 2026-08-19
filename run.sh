#!/usr/bin/env bash
# Start Gridiron locally. Creates a virtualenv on first run.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
VENV=${VENV:-.venv}

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtualenv in $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

if ! python -c "import gridiron" 2>/dev/null; then
  echo "==> Installing dependencies"
  pip install --upgrade pip >/dev/null
  pip install -e ".[dev]"
fi

if [ "${GPU:-auto}" != "off" ] && ! python -c "import torch" 2>/dev/null; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "==> NVIDIA GPU detected; installing CUDA build of torch"
    pip install torch --index-url https://download.pytorch.org/whl/cu121
  fi
fi

exec python -m gridiron.cli serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
