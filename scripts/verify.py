#!/usr/bin/env python3
"""Check that every dataset in datasets.json downloaded completely, measure its size, and write
<out>/<id>/_dataset_info.json with the dataset's metadata (links, resolution, image sizes, organism, organelles, ...).

Usage (from scripts/, after download_array.sbatch has finished):
  ./venv/bin/python verify.py --out $OUT                  # checks + sizes + _dataset_info.json in every dataset
  ./venv/bin/python verify.py --out $OUT --deep           # also re-hash md5s, CRC-test zips, read every crop voxel
  ./venv/bin/python verify.py --out $OUT --offline        # no network (skips the exact server-size comparison)
  ./venv/bin/python verify.py --out $OUT --only asem-crops empiar-10994

Checks per dataset kind
  all       _status.json present, not a test run, ok=true, no failures; no leftover .part/.badmd5/.tmp files
  files     every file present with the exact byte size (manifest, else the server's) (--deep: md5 too)
  http_dir  the server is listed again (same listing + filters as fetch_em.py) and every file must exist locally
            with the size the server reports (HEAD); --offline only compares the file count with _status.json
  gdown     every file of the Drive folder listing present and non-empty (--offline: the expected file count)
  crops     every crop has its sidecar, every source array, the right shape (clipped to the source volume) and all
            z-slabs done; missing source slices are reported (--deep: every voxel is read back, empty arrays flagged)
  manual    reports whether anything was put there by hand
  zips      (--deep) every member CRC-tested

Outputs
  <out>/<id>/_dataset_info.json   metadata.csv row + measured sizes + verification result + one entry per item
  <out>/verify_report.csv/.json   one row per dataset: status, GB on disk, problems
Item details come from <out>/items.csv (inventory.py, measured after download) when it exists, else from
../planned_items.csv (headers read before download). Exit code 1 if any dataset FAILED.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_em  # noqa: E402

log = logging.getLogger("verify")
LEFTOVER_RX = re.compile(r"\.(part|badmd5|tmp)$")
OWN_FILES = {"_status.json", "_status_test.json", "_dataset_info.json", "MANUAL_DOWNLOAD.txt"}
CATALOG_KEYS = [  # metadata.csv column -> key in the JSON
    ("dataset", "dataset_name"), ("used_for (from sources.csv)", "used_for"), ("dataset_link", "dataset_link"),
    ("paper_link", "paper_link"), ("imaging", "imaging"), ("organism", "organism"),
    ("organ_tissue_cell_type", "organ_tissue_cell_type"), ("organelles_in_data", "organelles"),
    ("labeled", "labeled"), ("label_classes", "label_classes"), ("xy_resolution_nm", "xy_resolution_nm"),
    ("z_resolution_nm", "z_resolution_nm"), ("xy_size_px", "xy_size_px"), ("z_size_slices", "z_size_slices"),
    ("total_size", "size_at_source"), ("download_plan", "download_plan"), ("notes", "notes")]
ITEM_DROP = {"dataset_id", "dataset", "imaging", "organism", "cell_type"}  # already in the dataset-level section


# ----------------------------------------------------------------------------- helpers

class Result:
    def __init__(self):
        self.fail, self.warn, self.info = [], [], []

    def status(self, manual=False):
        if self.fail:
            return "FAIL"
        if manual:
            return "MANUAL"
        return "WARN" if self.warn else "OK"


def walk(root):
    """{relative path: size} of every file below root (zarr crops included)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            full = os.path.join(dp, fn)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = os.path.getsize(full)
    return out


def disk_usage(root):
    """Allocated bytes (what counts against quota); None where the OS does not report blocks (Windows)."""
    tot = 0
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            st = os.lstat(os.path.join(dp, fn))
            if not hasattr(st, "st_blocks"):
                return None
            tot += st.st_blocks * 512
    return tot


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 24), b""):
            h.update(blk)
    return h.hexdigest()


def head_size(url):
    """Server size: -1 if missing, None if the server will not say (or keeps failing)."""
    try:
        return fetch_em.remote_size(url)
    except Exception as e:
        log.warning("no server size for %s (%r)", url, e)
        return None


def gb(n):
    return None if n is None else round(n / 1e9, 3)


# ----------------------------------------------------------------------------- per-kind checks

def check_status(root, res):
    p = os.path.join(root, "_status.json")
    if not os.path.exists(p):
        res.fail.append("_status.json missing: the download task never finished (re-submit it)")
        return None
    st = json.load(open(p, encoding="utf-8"))
    if st.get("test"):
        res.fail.append("_status.json is from a --test run")
    if st.get("failed"):
        res.fail.append(f"_status.json lists {len(st['failed'])} failed item(s): {st['failed'][:10]}")
        for k in st["failed"][:10]:  # the download error, so a FAIL can be diagnosed from the report alone
            res.fail.append(f"{k} error: {str(st.get('items', {}).get(k, '?'))[:300]}")
    elif not st.get("ok", False):
        res.fail.append("_status.json says ok=false")
    if os.path.exists(os.path.join(root, "_status_test.json")):
        res.warn.append("_status_test.json present: a smoke test wrote into this folder")
    return st


def check_files(flist, root, files, res, deep, offline, what="manifest files"):
    """Plain files (kind "files" and extra_files): present, exact size (manifest, else the server's), md5 (--deep).
    Returns the expected total bytes, or None if a size is unknown."""
    n_ok, expected, unknown = 0, 0, 0
    for f in flist:
        size = f.get("size")
        if size is None and not offline:
            size = head_size(f["url"])
            size = None if size == -1 else size
        unknown += size is None
        expected = None if size is None or expected is None else expected + size
        have = files.get(f["dest"])
        if have is None:
            res.fail.append(f"missing: {f['dest']}")
        elif size is not None and have != size:
            res.fail.append(f"size {have} != {size} expected: {f['dest']}")
        elif deep and f.get("md5") and md5_of(os.path.join(root, f["dest"])) != f["md5"]:
            res.fail.append(f"md5 differs from {f['md5']}: {f['dest']}")
        else:
            n_ok += 1
    if flist:
        res.info.append(f"{n_ok}/{len(flist)} {what} present with the exact size"
                        + (" and md5" if deep and any(f.get("md5") for f in flist) else "")
                        + (f" ({unknown} with no size known, only presence checked)" if unknown else ""))
    return expected


def check_http_dir(ds, root, files, status, res, offline, workers):
    local = {p: s for p, s in files.items() if p not in OWN_FILES and not LEFTOVER_RX.search(p)}
    if offline:
        n = (status or {}).get("n_files")
        if n is not None and len(local) < n:
            res.fail.append(f"{len(local)} files on disk, {n} selected at download time")
        res.info.append(f"{len(local)} files on disk (offline: server sizes not compared)")
        return None
    todo = fetch_em.http_dir_selection(ds)
    with cf.ThreadPoolExecutor(workers) as ex:
        sizes = list(ex.map(lambda t: head_size(t[1]), todo))
    expected, missing, wrong, unknown = 0, [], [], 0
    for (rel, _), size in zip(todo, sizes):
        have = local.get(rel)
        if size == -1:
            res.warn.append(f"listed but HEAD says 404 (server-side gap): {rel}")
            continue
        if size is None:
            unknown += 1
        else:
            expected += size
        if have is None:
            missing.append(rel)
        elif size is not None and have != size:
            wrong.append(f"{rel} ({have} != {size})")
    if missing:
        res.fail.append(f"{len(missing)} file(s) on the server missing locally, e.g. {missing[:10]}")
    if wrong:
        res.fail.append(f"{len(wrong)} file(s) with a different size than on the server, e.g. {wrong[:10]}")
    extra = sorted(set(local) - {r for r, _ in todo})
    if extra:
        res.warn.append(f"{len(extra)} local file(s) not on the server listing, e.g. {extra[:5]}")
    res.info.append(f"{len(todo) - len(missing) - len(wrong)}/{len(todo)} server files present with the exact "
                    f"server size" + (f" ({unknown} without a size from the server)" if unknown else ""))
    return None if unknown else expected


def check_gdown(ds, root, files, res, offline):
    """Compare with gdown's own folder listing (online); offline only the file count. .DS_Store files are ignored."""
    sub = ds.get("dest", "")
    pre = sub + "/" if sub else ""
    got = {p[len(pre):]: s for p, s in files.items() if p.startswith(pre) and p not in OWN_FILES
           and not LEFTOVER_RX.search(p) and not p.endswith(".DS_Store")}
    empty = [p for p, s in got.items() if s == 0]
    if empty:
        res.fail.append(f"{len(empty)} empty file(s), e.g. {empty[:5]}")
    listed = None
    if not offline:
        try:
            import gdown
            lst = fetch_em.with_retries(lambda: gdown.download_folder(
                url=ds["url"], output=os.path.join(root, sub), skip_download=True, quiet=True), ds["url"])
            listed = {f.path.replace("\\", "/") for f in lst or []} - {""}
            listed = {p for p in listed if not p.endswith(".DS_Store")}
        except Exception as e:  # old gdown without skip_download, or Drive rate limit
            res.warn.append(f"Google Drive folder could not be listed ({e!r:.200}); only the file count was checked")
    if listed:
        missing = sorted(listed - set(got))
        if missing:
            res.fail.append(f"{len(missing)} file(s) in the Drive folder missing locally: {missing[:10]}")
        res.info.append(f"{len(listed) - len(missing)}/{len(listed)} Drive files present (Drive gives no sizes or "
                        "checksums)")
    else:
        if len(got) < ds.get("expect_files", 1):
            res.fail.append(f"{len(got)} files, expected {ds.get('expect_files')}")
        res.info.append(f"{len(got)} files (expected {ds.get('expect_files', '?')}); Drive gives no checksums")


def _zarray(path):
    p = os.path.join(path, ".zarray")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def check_crops(ds, root, res, deep):
    """Returns the crop summaries for the JSON."""
    out = []
    for crop in ds["crops"]:
        name = crop["name"]
        side = os.path.join(root, name + ".json")
        zpath = os.path.join(root, name + ".ome.zarr")
        if not os.path.exists(side):
            res.fail.append(f"{name}: sidecar missing (crop not finished)")
            continue
        sc = json.load(open(side, encoding="utf-8"))
        if sc.get("test_mode"):
            res.fail.append(f"{name}: is a --test crop")
        prog_p = os.path.join(zpath, ".progress.json")
        progress = json.load(open(prog_p, encoding="utf-8")) if os.path.exists(prog_p) else {}
        summary = {"crop": name, "offset_zyx_vox": crop["offset_zyx"], "requested_size_zyx": crop["size_zyx"],
                   "voxel_nm_zyx": crop.get("voxel_nm_zyx"), "path": os.path.basename(zpath), "arrays": []}
        for k in ("note", "labels_note"):
            if k in crop:
                summary[k] = crop[k]
        for sspec in crop["sources"]:
            an = sspec["name"]
            a = sc.get("arrays", {}).get(an)
            if a is None:
                res.fail.append(f"{name}/{an}: not in sidecar")
                continue
            off = sspec.get("offset_zyx", crop["offset_zyx"])
            want = [max(0, min(s, n - o)) for o, s, n in
                    zip(off, sspec.get("size_zyx", crop["size_zyx"]), a["source_shape_zyx"])]
            za = _zarray(os.path.join(zpath, an, "s0"))
            if za is None:
                res.fail.append(f"{name}/{an}: zarr array missing")
                continue
            if list(za["shape"]) != want or list(a["shape_zyx"]) != want:
                res.fail.append(f"{name}/{an}: shape {za['shape']} (sidecar {a['shape_zyx']}) != expected {want}")
            nslabs = -(-za["shape"][0] // za["chunks"][0])
            done = set(progress.get(an, {}).get("done", []))
            if progress and len(done & set(range(nslabs))) != nslabs:
                res.fail.append(f"{name}/{an}: {len(done)}/{nslabs} z-slabs written")
            miss = a.get("missing_source_slices") or []
            if miss:
                res.warn.append(f"{name}/{an}: {len(miss)} source slice(s) absent on the server, written as 0: "
                                f"{miss[:10]}")
            nbytes = int(np.prod(za["shape"])) * np.dtype(za["dtype"]).itemsize
            entry = {"array": an, "role": "label" if an.startswith("labels/") else "raw", "shape_zyx": za["shape"],
                     "dtype": za["dtype"], "voxel_nm_zyx": a["voxel_nm_zyx"], "offset_vox_zyx": a["offset_vox_zyx"],
                     "source_shape_zyx": a["source_shape_zyx"], "uncompressed_gb": gb(nbytes),
                     "source_url": sspec.get("url") or sspec.get("url_template") or sspec.get("zip_url"),
                     "missing_source_slices": len(miss)}
            if "gt_crops" in sspec:
                entry["gt_crops"] = sspec["gt_crops"]
            if sspec.get("note"):
                entry["note"] = sspec["note"]
            if deep:
                entry.update(_read_back(zpath, an, name, res))
            summary["arrays"].append(entry)
        out.append(summary)
    names = {c["name"] for c in ds["crops"]}
    stray = [fn for fn in os.listdir(root) if fn.endswith(".ome.zarr") and fn[:-9] not in names]
    if stray:
        res.warn.append(f"crop folder(s) not in the manifest: {stray}")
    res.info.append(f"{sum(1 for c in out if c['arrays'])}/{len(ds['crops'])} crops checked")
    return out


def _read_back(zpath, an, name, res):
    """Decompress every chunk; flag arrays that are entirely zero."""
    import zarr
    arr = zarr.open_array(os.path.join(zpath, an, "s0"), mode="r")
    nz, lo, hi, step = 0, None, None, max(1, min(arr.chunks[0], 16))
    try:
        for z in range(0, arr.shape[0], step):
            a = arr[z:z + step]
            nz += int(np.count_nonzero(a))
            lo = a.min() if lo is None else min(lo, a.min())
            hi = a.max() if hi is None else max(hi, a.max())
    except Exception as e:
        res.fail.append(f"{name}/{an}: unreadable at z={z}: {e!r}")
        return {"read_back": "FAILED"}
    frac = nz / max(1, int(np.prod(arr.shape)))
    if nz == 0:
        (res.warn if an.startswith("labels/") else res.fail).append(f"{name}/{an}: every voxel is 0")
    return {"read_back": "ok", "nonzero_fraction": round(frac, 6), "min": float(lo), "max": float(hi)}


def check_zips(root, files, res):
    bad = []
    zips = [p for p in files if p.lower().endswith(".zip")]
    for p in zips:
        try:
            with zipfile.ZipFile(os.path.join(root, p)) as z:
                m = z.testzip()
            if m:
                bad.append(f"{p} (member {m})")
        except zipfile.BadZipFile as e:
            bad.append(f"{p} ({e})")
    if bad:
        res.fail.append(f"zip CRC errors: {bad}")
    elif zips:
        res.info.append(f"{len(zips)} zip(s) CRC-tested ok")


# ----------------------------------------------------------------------------- metadata JSON

def load_catalog(path):
    cat = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        for mid in re.split(r"\s*;\s*", r.get("manifest_ids", "")):
            if mid:
                cat[mid] = r
    return cat


def load_items(path):
    items = {}
    if path and os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            items.setdefault(r["dataset_id"], []).append(r)
    return items


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except (TypeError, ValueError):
        return v


def item_summary(rows):
    """Distinct voxel sizes, image-size range and label classes over the raw/label items."""
    vox, classes, roles = {}, set(), {}
    shapes = {"z": [], "y": [], "x": []}
    for r in rows:
        roles[r["role"]] = roles.get(r["role"], 0) + 1
        if r["role"] not in ("raw", "label"):
            continue
        key = tuple(_num(r.get(f"voxel_{ax}_nm")) or None for ax in "zyx")  # None = not known
        v = vox.setdefault(key, {"z_nm": key[0], "y_nm": key[1], "x_nm": key[2],
                                 "n_items": 0, "source": r.get("voxel_source", "")})
        v["n_items"] += 1
        for ax in "zyx":
            for part in re.split(r"[-–]", r.get(f"shape_{ax}") or ""):
                if part.strip().isdigit():
                    shapes[ax].append(int(part))
        for c in re.split(r"\s*[;,]\s*", r.get("label_classes") or ""):
            if c:
                classes.add(c)
    return {
        "items_by_role": roles,
        "voxel_sizes_nm": sorted(vox.values(), key=lambda v: -v["n_items"]),
        "image_size_range_px": {ax: [min(v), max(v)] for ax, v in shapes.items() if v},
        "label_classes_found": sorted(classes),
    }


def write_info(ds, root, cat_row, items, items_src, res, measured, crops):
    info = {"dataset_id": ds["id"], "title": ds.get("title", ""),
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "metadata": {k2: (cat_row or {}).get(k1, "") for k1, k2 in CATALOG_KEYS},
            "download": {"kind": ds["kind"], "source_urls": _source_urls(ds), "folder": root},
            "on_disk": measured,
            "verification": {"status": res.status(ds["kind"] == "manual"), "problems": res.fail,
                             "warnings": res.warn, "checks": res.info},
            "measured_summary": item_summary(items),
            "items_source": items_src,
            "items": [{k: _num(v) for k, v in r.items() if v not in ("", None) and k not in ITEM_DROP} for r in items]}
    if cat_row is None:
        info["metadata"]["notes"] = "no row for this id in metadata.csv"
    if crops is not None:
        info["crops"] = crops
    if ds.get("note"):
        info["download"]["note"] = ds["note"]
    with open(os.path.join(root, "_dataset_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, ensure_ascii=False)
    return info


def _source_urls(ds):
    if ds["kind"] == "files":
        return [f["url"] for f in ds["files"]]
    if ds["kind"] == "crops":
        urls = []
        for c in ds["crops"]:
            for s in c["sources"]:
                u = s.get("url") or s.get("url_template") or s.get("zip_url")
                if u:
                    u = re.sub(r"/(s\d|\d-\d-\d)/?$", "/", u)  # array -> its group, to keep the list short
                    if u not in urls:
                        urls.append(u)
        return urls
    return ds.get("urls") or ([ds["url"]] if ds.get("url") else [])


# ----------------------------------------------------------------------------- main

def verify_dataset(ds, args, cat, items_all, items_src):
    did = ds["id"]
    root = os.path.join(args.out, did)
    res = Result()
    if not os.path.isdir(root):
        res.fail.append(f"folder missing: {root}")
        return {"id": did, "status": "FAIL", "problems": res.fail}
    files = walk(root)
    st = None if ds["kind"] == "manual" else check_status(root, res)
    left = [p for p in files if LEFTOVER_RX.search(p)]
    if left:
        res.warn.append(f"{len(left)} leftover partial/temporary file(s) (safe to delete): {left[:10]}")
    expected, crops = None, None
    kind = ds["kind"]
    if kind == "files":
        expected = check_files(ds["files"], root, files, res, args.deep, args.offline)
    elif kind == "http_dir":
        expected = check_http_dir(ds, root, files, st, res, args.offline, args.workers)
    elif kind == "gdown_folder":
        check_gdown(ds, root, files, res, args.offline)
    elif kind == "crops":
        crops = check_crops(ds, root, res, args.deep)  # compressed: no exact on-disk size to expect
    elif kind == "manual":
        mine = [p for p in files if p not in OWN_FILES]
        res.info.append(f"manual download: {len(mine)} file(s) placed here by hand" if mine else
                        "manual download: nothing here yet (see MANUAL_DOWNLOAD.txt)")
    check_files(ds.get("extra_files", []), root, files, res, args.deep, args.offline, "extra files")
    if args.deep:
        check_zips(root, files, res)
    data_bytes = sum(s for p, s in files.items() if p not in OWN_FILES and not LEFTOVER_RX.search(p))
    measured = {"bytes": data_bytes, "gb": gb(data_bytes), "n_files": len(files),
                "disk_usage_gb": gb(disk_usage(root)), "expected_gb_exact": gb(expected)}
    if crops is not None:
        measured["crops_uncompressed_gb"] = gb(sum(a["uncompressed_gb"] * 1e9 for c in crops for a in c["arrays"]))
    info = write_info(ds, root, cat.get(did), items_all.get(did, []), items_src, res, measured, crops)
    return {"id": did, "kind": kind, "status": info["verification"]["status"], "gb_on_disk": measured["gb"],
            "disk_usage_gb": measured["disk_usage_gb"], "expected_gb": measured["expected_gb_exact"],
            "n_files": len(files), "n_items": len(info["items"]), "problems": res.fail, "warnings": res.warn,
            "checks": res.info}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="download root (as given to fetch_em.py --out)")
    ap.add_argument("--manifest", default=os.path.join(HERE, "datasets.json"))
    ap.add_argument("--metadata", default=os.path.join(HERE, "..", "metadata.csv"))
    ap.add_argument("--items", help="items.csv from inventory.py (default: <out>/items.csv, else ../planned_items.csv)")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--deep", action="store_true", help="re-hash md5s, CRC-test zips, read every crop voxel")
    ap.add_argument("--offline", action="store_true", help="do not contact servers")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("fetch_em").setLevel(logging.WARNING)

    man = json.load(open(args.manifest, encoding="utf-8"))["datasets"]
    if args.only:
        bad = set(args.only) - {d["id"] for d in man}
        if bad:
            ap.error(f"unknown dataset ids: {sorted(bad)}")
        man = [d for d in man if d["id"] in args.only]
    cat = load_catalog(args.metadata)
    items_path = args.items or os.path.join(args.out, "items.csv")
    if os.path.exists(items_path):
        items_src = f"{os.path.basename(items_path)} (inventory.py, measured on the downloaded files)"
    else:
        items_path = os.path.join(HERE, "..", "planned_items.csv")
        items_src = "planned_items.csv (file headers read BEFORE download; run inventory.py for measured values)"
    items_all = load_items(items_path)
    log.info("items from %s", items_path)

    rows = []
    for ds in man:
        log.info("=== %s", ds["id"])
        try:
            r = verify_dataset(ds, args, cat, items_all, items_src)
        except Exception as e:
            log.exception("%s: verification crashed", ds["id"])
            r = {"id": ds["id"], "kind": ds["kind"], "status": "FAIL", "problems": [f"verify crashed: {e!r}"]}
        log.info("%s: %s %s", ds["id"], r["status"], "; ".join(r["problems"])[:500])
        rows.append(r)

    cols = ["id", "kind", "status", "gb_on_disk", "disk_usage_gb", "expected_gb", "n_files", "n_items",
            "problems", "warnings"]
    csv_rows = [{**r, "problems": " | ".join(r.get("problems", [])), "warnings": " | ".join(r.get("warnings", []))}
                for r in rows]
    json_rows = rows
    csv_path = os.path.join(args.out, "verify_report.csv")
    json_path = os.path.join(args.out, "verify_report.json")
    if args.only:
        # --only re-checks a few datasets: replace their rows in the existing report and keep all the others
        order = [d["id"] for d in json.load(open(args.manifest, encoding="utf-8"))["datasets"]]
        pos = {did: i for i, did in enumerate(order)}
        rank = lambda r: pos.get(r["id"], len(order))  # noqa: E731
        if os.path.exists(csv_path):
            with open(csv_path, newline="", encoding="utf-8") as f:
                old = [r for r in csv.DictReader(f) if r.get("id") not in args.only]
            csv_rows = sorted(old + csv_rows, key=rank)
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    old = [r for r in json.load(f).get("datasets", []) if r.get("id") not in args.only]
                json_rows = sorted(old + rows, key=rank)
            except (OSError, ValueError) as e:
                log.warning("could not merge into %s (%r); writing only the checked datasets", json_path, e)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(csv_rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "deep": args.deep, "offline": args.offline, "items_source": items_src,
                   "updated": args.only or "all", "datasets": json_rows},
                  f, indent=1, ensure_ascii=False)

    print(f"\n{'dataset':28s} {'status':7s} {'GB':>8s}  notes")
    tot = 0.0
    for r in rows:
        tot += r.get("gb_on_disk") or 0
        note = "; ".join(r.get("problems") or r.get("warnings") or [])
        print(f"{r['id']:28s} {r['status']:7s} {r.get('gb_on_disk') or 0:8.2f}  {note[:150]}")
    print(f"{'total':28s} {'':7s} {tot:8.2f}")
    print(f"\nreport: {os.path.join(args.out, 'verify_report.csv')}; per dataset: <id>/_dataset_info.json")
    return 1 if any(r["status"] == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
