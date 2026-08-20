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

if [ "${GPU:-auto}" != "off" ] && ! python -c "import cupy" 2>/dev/null; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "==> NVIDIA GPU detected; installing CuPy"
    # CuPy ships per-CUDA-major wheels; pick from the driver's reported version.
    CUDA_MAJOR=$(nvidia-smi | grep -oE "CUDA Version: [0-9]+" | grep -oE "[0-9]+$" || echo 12)
    if [ "$CUDA_MAJOR" -ge 12 ] 2>/dev/null; then
      pip install cupy-cuda12x || echo "==> CuPy install failed; continuing on CPU"
    else
      pip install cupy-cuda11x || echo "==> CuPy install failed; continuing on CPU"
    fi
  fi
fi

# LAN=1 ./run.sh  is the same as  ./run.sh --lan  (serve to phones on this wifi)
EXTRA=()
if [ "${LAN:-0}" = "1" ]; then EXTRA+=(--lan); fi

exec python -m gridiron.cli serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "${EXTRA[@]+"${EXTRA[@]}"}" "$@"
