#!/usr/bin/env bash
#SBATCH --job-name=em-verify
#SBATCH --partition=short
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=logs/%x_%j.out
# Add --account=... here if your cluster needs one.
#
# Run after download_array.sbatch has finished (from this scripts/ directory, same OUT as the download):
#   sbatch verify.sbatch                         # OUT defaults to ../../em_datasets (next to the repo)
#   OUT=/other/storage/em_datasets sbatch verify.sbatch
#   SKIP_INVENTORY=1 sbatch verify.sbatch        # reuse an existing $OUT/items.csv
# 1. inventory.py writes $OUT/items.csv (one row per item, measured on the files; takes hours, reads everything)
# 2. verify.py --deep checks every dataset, re-hashes md5s, CRC-tests zips, reads every crop voxel, and writes
#    $OUT/<id>/_dataset_info.json plus $OUT/verify_report.csv
set -uo pipefail

HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${OUT:-$(cd "$HERE/../.." && pwd)/em_datasets}"
ENV_DIR="${ENV_DIR:-$HERE/venv}"
PY="$ENV_DIR/bin/python"
if [ ! -f "$HERE/verify.py" ] || [ ! -x "$PY" ] || [ ! -d "$OUT" ]; then
    echo "ERROR: submit from the scripts/ folder after setup_env.sh, with OUT=$OUT pointing at the downloads" >&2
    exit 2
fi
echo "host=$(hostname) out=$OUT start=$(date -Is)"

if [ -z "${SKIP_INVENTORY:-}" ] || [ ! -f "$OUT/items.csv" ]; then
    "$PY" -u "$HERE/inventory.py" --manifest "$HERE/datasets.json" --metadata "$HERE/../metadata.csv" \
        --out "$OUT" --csv "$OUT/items.csv" \
        || echo "WARNING: inventory.py failed; _dataset_info.json will use planned_items.csv for item details"
fi

"$PY" -u "$HERE/verify.py" --manifest "$HERE/datasets.json" --metadata "$HERE/../metadata.csv" \
    --out "$OUT" --deep --workers 16
rc=$?
echo "end=$(date -Is) rc=$rc (0 = every dataset OK/WARN/MANUAL; see $OUT/verify_report.csv)"
exit $rc
