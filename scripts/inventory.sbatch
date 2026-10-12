#!/usr/bin/env bash
#SBATCH --job-name=em-inventory
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --time=1-00:00:00
#SBATCH --output=logs/%x_%j.out
# Adjust --partition / --account to your cluster, e.g.:
##SBATCH --partition=transfer
##SBATCH --account=myproject
#
# Run after download_array.sbatch has finished (from this directory):
#   OUT=/path/to/big/storage/em_datasets sbatch inventory.sbatch
# Writes $OUT/items.csv: one row per downloaded item with shape, voxel size, label and intensity statistics.
set -euo pipefail

HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${OUT:?set OUT=/path/to/output (the same as for download_array.sbatch)}"
ENV_DIR="${ENV_DIR:-$HERE/venv}"

"$ENV_DIR/bin/python" "$HERE/inventory.py" --manifest "$HERE/datasets.json" --metadata "$HERE/../metadata.csv" \
    --out "$OUT" --csv "$OUT/items.csv"
