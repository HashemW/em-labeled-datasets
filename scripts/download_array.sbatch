#!/usr/bin/env bash
#SBATCH --job-name=em-fetch
#SBATCH --array=0-22%4            # one task per dataset in datasets.json (python fetch_em.py --list); max 4 at once
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=2-00:00:00         # the EMPIAR-10791 liver crops are the slowest (~100 GB transfer each)
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.out
# Adjust --partition / --account to your cluster, e.g.:
##SBATCH --partition=transfer
##SBATCH --account=myproject
#
# Usage (from this directory):
#   mkdir -p logs
#   OUT=/path/to/big/storage/em_datasets sbatch download_array.sbatch
#   # re-run only some datasets, e.g. 17 and 19:
#   OUT=/path/... sbatch --array=17,19 download_array.sbatch
#
# Every task is resumable: re-submitting skips completed files / crop slabs.
set -euo pipefail

HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${OUT:?set OUT=/path/to/output, e.g. OUT=/scratch/me/em_datasets sbatch download_array.sbatch}"
ENV_DIR="${ENV_DIR:-$HERE/venv}"
PY="$ENV_DIR/bin/python"

echo "host=$(hostname) task=${SLURM_ARRAY_TASK_ID} out=$OUT start=$(date -Is)"
"$PY" "$HERE/fetch_em.py" --list | awk -v i="${SLURM_ARRAY_TASK_ID}" '$1==i'

rc=0
"$PY" "$HERE/fetch_em.py" \
    --manifest "$HERE/datasets.json" \
    --out "$OUT" \
    --task-index "${SLURM_ARRAY_TASK_ID}" \
    --workers $(( ${SLURM_CPUS_PER_TASK:-8} * 2 )) || rc=$?
echo "end=$(date -Is) rc=$rc"
exit $rc
