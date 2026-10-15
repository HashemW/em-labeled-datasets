#!/usr/bin/env bash
#SBATCH --job-name=em-verify-one
#SBATCH --partition=short
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=logs/%x_%j.out
# Add --account=... here if your cluster needs one.
#
# Full check of ONE dataset (or a few), e.g. after re-downloading it. Same checks as verify.sbatch, but only for
# the datasets you name. Give the task number from download_array.sbatch, or the dataset id (fetch_em.py --list):
#   sbatch verify_one.sbatch 17                          # = empiar-13420-crops
#   sbatch verify_one.sbatch empiar-13420-crops
#   sbatch verify_one.sbatch 3 17                        # several at once
#   OUT=/other/storage/em_datasets sbatch verify_one.sbatch 17
#   SKIP_INVENTORY=1 sbatch verify_one.sbatch 17         # keep the existing items.csv rows of that dataset
# Only that dataset's rows in $OUT/items.csv and $OUT/verify_report.csv are replaced; all other rows are kept.
set -uo pipefail

HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${OUT:-$(cd "$HERE/../.." && pwd)/em_datasets}"
ENV_DIR="${ENV_DIR:-$HERE/venv}"
PY="$ENV_DIR/bin/python"
if [ ! -f "$HERE/verify.py" ] || [ ! -x "$PY" ] || [ ! -d "$OUT" ]; then
    echo "ERROR: submit from the scripts/ folder after setup_env.sh, with OUT=$OUT pointing at the downloads" >&2
    exit 2
fi
if [ $# -eq 0 ]; then
    echo "ERROR: name the dataset(s), e.g.: sbatch verify_one.sbatch 17   (task numbers: $PY fetch_em.py --list)" >&2
    exit 2
fi

# task numbers -> dataset ids (same order as download_array.sbatch)
IDS=$("$PY" - "$HERE/datasets.json" "$@" <<'EOF'
import json, sys
ids = [d["id"] for d in json.load(open(sys.argv[1], encoding="utf-8"))["datasets"]]
out = []
for a in sys.argv[2:]:
    if a.isdigit() and int(a) < len(ids):
        out.append(ids[int(a)])
    elif a in ids:
        out.append(a)
    else:
        sys.exit(f"ERROR: unknown dataset {a!r}; use a task number 0-{len(ids) - 1} or one of: {', '.join(ids)}")
print(" ".join(out))
EOF
) || exit 2
echo "host=$(hostname) out=$OUT datasets=$IDS start=$(date -Is)"

if [ -z "${SKIP_INVENTORY:-}" ] && [ -f "$OUT/items.csv" ]; then  # no items.csv yet: verify uses planned_items.csv
    # shellcheck disable=SC2086
    "$PY" -u "$HERE/inventory.py" --manifest "$HERE/datasets.json" --metadata "$HERE/../metadata.csv" \
        --out "$OUT" --csv "$OUT/items.csv" --only $IDS \
        || echo "WARNING: inventory.py failed; _dataset_info.json will use older item details"
fi

# shellcheck disable=SC2086
"$PY" -u "$HERE/verify.py" --manifest "$HERE/datasets.json" --metadata "$HERE/../metadata.csv" \
    --out "$OUT" --deep --workers 16 --only $IDS
rc=$?
echo "end=$(date -Is) rc=$rc (0 = OK/WARN/MANUAL; the rows of $IDS in $OUT/verify_report.csv are updated)"
exit $rc
