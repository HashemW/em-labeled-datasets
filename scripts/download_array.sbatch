#!/usr/bin/env bash
#SBATCH --job-name=em-fetch
#SBATCH --array=0-20%4            # one task per dataset in datasets.json (python fetch_em.py --list); max 4 at once
#SBATCH --partition=short
#SBATCH --time=24:00:00           # tasks are resumable: one that hits the limit is just re-submitted (see below)
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.out
# Add --account=... here if your cluster needs one.
#
# Usage (from this scripts/ directory):
#   mkdir -p logs
#   sbatch download_array.sbatch                         # data goes to ../../em_datasets (next to the repo)
#   OUT=/other/storage/em_datasets sbatch download_array.sbatch
#   # re-run only some datasets, e.g. 16 and 18 (same OUT as before):
#   sbatch --array=16,18 download_array.sbatch
#
# Every task is resumable: re-submitting skips completed files / crop slabs.
set -euo pipefail

HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${OUT:-$(cd "$HERE/../.." && pwd)/em_datasets}"
ENV_DIR="${ENV_DIR:-$HERE/venv}"
PY="$ENV_DIR/bin/python"
if [ ! -f "$HERE/fetch_em.py" ] || [ ! -x "$PY" ]; then
    echo "ERROR: submit from the scripts/ folder (found HERE=$HERE) after running setup_env.sh" >&2
    exit 2
fi
mkdir -p "$OUT"

echo "host=$(hostname) task=${SLURM_ARRAY_TASK_ID} out=$OUT start=$(date -Is)"
"$PY" "$HERE/fetch_em.py" --list | awk -v i="${SLURM_ARRAY_TASK_ID}" '$1==i'

rc=0
"$PY" -u "$HERE/fetch_em.py" \
    --manifest "$HERE/datasets.json" \
    --out "$OUT" \
    --task-index "${SLURM_ARRAY_TASK_ID}" \
    --workers $(( ${SLURM_CPUS_PER_TASK:-8} * 2 )) || rc=$?
echo "end=$(date -Is) rc=$rc"
exit $rc
