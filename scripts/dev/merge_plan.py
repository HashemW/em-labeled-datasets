"""Build ../planned_items.csv from per-dataset `inventory.py --remote` CSVs.

    python dev/merge_plan.py --plan-dir <dir with <dataset_id>.csv> [--local full_downloads.csv] [--base ../planned_items.csv]

Order follows datasets.json. For each dataset the rows come from (first match): --local (an inventory of a full
download, e.g. platelet-guay), rows in --base that came from a full download, <plan-dir>/<id>.csv, then the
other rows already in --base (so re-running a few datasets only needs their CSVs).
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import inventory as I  # noqa: E402


def load(path):
    out = {}
    if path and os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            out.setdefault(r["dataset_id"], []).append(r)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan-dir", required=True)
    ap.add_argument("--local")
    ap.add_argument("--base", default=os.path.join(HERE, "..", "planned_items.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "planned_items.csv"))
    a = ap.parse_args()
    man = json.load(open(os.path.join(HERE, "datasets.json"), encoding="utf-8"))["datasets"]
    local, base = load(a.local), load(a.base)
    for rs in local.values():
        for r in rs:
            if "full test download" not in (r.get("notes") or ""):
                r["notes"] = "; ".join(filter(None, [r.get("notes"), "computed from a full test download (2026-10-12)"]))
    rows, src = [], {}
    for d in man:
        did = d["id"]
        p = os.path.join(a.plan_dir, did + ".csv")
        full_base = did in base and any("full test download" in (r.get("notes") or "") for r in base[did])
        if did in local:
            rows += local[did]; src[did] = "local"
        elif full_base:  # complete statistics from a full download beat a remote header pass
            rows += base[did]; src[did] = "base (full download)"
        elif os.path.exists(p):
            rows += list(csv.DictReader(open(p, encoding="utf-8"))); src[did] = "plan-dir"
        elif did in base:
            rows += base[did]; src[did] = "base"
        else:
            src[did] = "MISSING"
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=I.FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} rows -> {a.out}")
    for did, s in src.items():
        print(f"  {did:22s} {s}")


if __name__ == "__main__":
    main()
