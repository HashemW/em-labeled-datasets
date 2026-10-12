#!/usr/bin/env bash
# Create the Python environment for fetch_em.py (run once, on a node with internet access).
#   bash setup_env.sh [ENV_DIR]        default ENV_DIR: ./venv next to this script
# If your cluster uses modules, load a Python >= 3.9 first, e.g.:  module load python/3.11
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${1:-$HERE/venv}"
PY="${PYTHON:-python3}"

"$PY" - <<'EOF'
import sys
assert sys.version_info >= (3, 9), f"Python >= 3.9 required, found {sys.version.split()[0]}"
print("Python", sys.version.split()[0], "OK")
EOF

"$PY" -m venv "$ENV_DIR"
"$ENV_DIR/bin/python" -m pip install --upgrade pip
"$ENV_DIR/bin/python" -m pip install -r "$HERE/requirements.txt"
"$ENV_DIR/bin/python" -c "import numpy, requests, zarr, numcodecs, tifffile, imagecodecs, gdown, h5py, PIL; print('zarr', zarr.__version__, '- environment OK')"
echo "Environment ready: $ENV_DIR"
