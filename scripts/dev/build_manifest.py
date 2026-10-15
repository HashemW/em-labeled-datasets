#!/usr/bin/env python3
"""Regenerate ../datasets.json. Developer tool: queries Zenodo/figshare/4TU/S3 for current file lists + md5s.

Crops are at most W^3 = 1000^3 voxels (decision 2026-10-12; was 2000^3). Placement (see README.md, "How crop
positions were chosen"):
* label boxes (OpenOrganelle GT crops): enough windows to contain every box completely, computed here from the
  bucket's GT metadata with a deterministic greedy cover;
* sparse painted labels (ASEM): the volume is tiled into equal tiles <= W, and every tile holding labeled
  chunks is kept (labeled = stored chunk larger than an all-zero chunk);
* whole-volume labels (EMPIAR-10791, EMPIAR-13420, FAST-EM GT region): one window per volume, hard-coded
  from the label-density analysis of 2026-10-12.
"""
import itertools
import json
import os
import re
import xml.etree.ElementTree as ET

import requests

S = requests.Session()
EMPIAR = "https://ftp.ebi.ac.uk/empiar/world_availability/"
COSEM = "https://janelia-cosem-datasets.s3.amazonaws.com/"
ASEM = "https://asem-project.s3.amazonaws.com/datasets/"
WK = "https://webknossos.tnw.tudelft.nl/data/zarr/6672e7da010000f100a1be53/"
L10791 = (EMPIAR + "10791/data/Parlakgul%20et%20al%20-%20Regulation%20of%20liver%20subcellular%20architecture%20"
          "controls%20metabolic%20homeostasis/FIB-SEM%20Raw%20and%20Segmentation%20Data/")
BIG = [10 ** 6] * 3  # "whole array" (clipped to the source extent)
W = 1000  # crop edge in voxels


def s3_list(bucket, prefix, delimiter=None):
    """[(key, size)] and [common prefixes] under prefix in a public S3 bucket."""
    keys, prefixes, tok = [], [], None
    while True:
        p = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            p["delimiter"] = delimiter
        if tok:
            p["continuation-token"] = tok
        r = S.get(f"https://{bucket}.s3.amazonaws.com/", params=p, timeout=120)
        r.raise_for_status()
        t = ET.fromstring(r.content)
        ns = {"s": t.tag.split("}")[0][1:]}
        keys += [(c.find("s:Key", ns).text, int(c.find("s:Size", ns).text)) for c in t.findall("s:Contents", ns)]
        prefixes += [c.find("s:Prefix", ns).text for c in t.findall("s:CommonPrefixes", ns)]
        if t.find("s:IsTruncated", ns).text != "true":
            return keys, prefixes
        tok = t.find("s:NextContinuationToken", ns).text


def greedy_windows(boxes, shape, w=W):
    """Cover boxes {name: (start_zyx, size_zyx)} with windows of edge <= w inside shape; each box ends up fully
    inside one window. Candidates are windows aligned to a box corner; pick the one holding most uncovered boxes,
    ties broken by position, until all are covered. Returns [(offset_zyx, [box names])]."""
    ws = [min(w, n) for n in shape]
    cands = set()
    for st, sz in boxes.values():
        for corner in itertools.product((0, 1), repeat=3):
            cands.add(tuple(max(0, min(n - wa, s if c == 0 else s + z - wa))
                            for s, z, c, wa, n in zip(st, sz, corner, ws, shape)))

    def inside(o, b):
        return all(o[a] <= b[0][a] and b[0][a] + b[1][a] <= o[a] + ws[a] for a in range(3))
    rem, out = set(boxes), []
    while rem:
        best = max(sorted(cands), key=lambda o: sum(inside(o, boxes[n]) for n in rem))
        got = sorted(n for n in rem if inside(best, boxes[n]))
        if not got:
            raise RuntimeError(f"boxes {sorted(rem)} do not fit in a {ws} window")
        out.append((list(best), got))
        rem -= set(got)
    return out


def tiles(n, w=W):
    """Split [0, n) into equal parts of length <= w: [(start, length)]."""
    k = -(-n // w)
    edges = [round(i * n / k) for i in range(k + 1)]
    return [(a, b - a) for a, b in zip(edges, edges[1:])]


def zenodo(rec, dest=""):
    d = S.get(f"https://zenodo.org/api/records/{rec}", timeout=60).json()
    return [{"url": f["links"]["self"], "dest": dest + f["key"], "size": f["size"],
             "md5": f["checksum"].split(":", 1)[1]} for f in d["files"]]


def figshare(art):
    d = S.get(f"https://api.figshare.com/v2/articles/{art}/files", timeout=60).json()
    return [{"url": f["download_url"], "dest": f["name"], "size": f["size"], "md5": f["computed_md5"]} for f in d]


def fourtu(uuid):
    d = S.get(f"https://data.4tu.nl/v2/articles/{uuid}/files", timeout=60).json()
    return [{"url": f["download_url"], "dest": f["name"], "size": f["size"], "md5": f["computed_md5"]} for f in d]


def cosem_crops(ds):
    """One W^3 window per group of GT crops, so that every GT crop of the volume is downloaded with its raw."""
    base = f"{COSEM}{ds}/{ds}.zarr/recon-1/"
    em = S.get(base + "em/fibsem-uint8/.zattrs").json()["multiscales"][0]["datasets"]
    vox = [d for d in em if d["path"] == "s0"][0]["coordinateTransformations"][0]["scale"]
    shape = S.get(base + "em/fibsem-uint8/s0/.zarray").json()["shape"]
    _, prefixes = s3_list("janelia-cosem-datasets", f"{ds}/{ds}.zarr/recon-1/labels/groundtruth/", "/")
    gt, boxes = {}, {}
    for p in sorted(prefixes, key=lambda p: int(re.search(r"crop(\d+)/$", p).group(1))):
        c = p.rstrip("/").rsplit("/", 1)[1]
        ms = S.get(f"{base}labels/groundtruth/{c}/all/.zattrs").json()["multiscales"][0]["datasets"]
        ct = {d["path"]: d["coordinateTransformations"] for d in ms}
        gt[c] = ct
        sc, tr = ct["s1"][0]["scale"], ct["s1"][1]["translation"]
        if [round(s, 3) for s in sc] != [round(v, 3) for v in vox]:
            raise RuntimeError(f"{ds}/{c}: s1 scale {sc} is not the raw grid {vox}")
        n1 = S.get(f"{base}labels/groundtruth/{c}/all/s1/.zarray").json()["shape"]
        boxes[c] = ([round(t / s) for t, s in zip(tr, sc)], n1)
    crops = []
    for i, (off, names) in enumerate(greedy_windows(boxes, shape), 1):
        srcs = [{"name": "raw", "type": "zarr2", "url": base + "em/fibsem-uint8/s0", "voxel_nm_zyx": vox}]
        for c in names:
            for lvl, tag in (("s1", ""), ("s0", "_s0")):
                sc, tr = gt[c][lvl][0]["scale"], gt[c][lvl][1]["translation"]
                srcs.append({"name": f"labels/gt_{c}{tag}", "type": "zarr2",
                             "url": f"{base}labels/groundtruth/{c}/all/{lvl}",
                             "offset_zyx": [0, 0, 0], "size_zyx": BIG, "voxel_nm_zyx": sc,
                             "world_offset_vox_zyx": [round(t / s, 3) for t, s in zip(tr, sc)],
                             "note": ("dense multi-class GT ('all'), aligned with raw (EM s0 grid)" if lvl == "s1" else
                                      "dense multi-class GT at native annotation resolution (2x finer than raw)")})
        crops.append({"name": f"{ds}_w{i:02d}", "offset_zyx": off, "size_zyx": [W, W, W], "voxel_nm_zyx": vox,
                      "sources": srcs, "gt_crops": names,
                      "note": f"window {i} of the greedy cover of all {len(boxes)} GT crops; holds {names} completely"})
    return crops


def tiff_stack(name, template, first, count, vox, **kw):
    return dict({"name": name, "type": "tiff_slices", "url_template": template, "first": first, "count": count,
                 "voxel_nm_zyx": vox}, **kw)


def liver(spec_id, label, raw_tpl, raw_first, count, zips, offset, extra_num_regex=None):
    srcs = [tiff_stack("raw", raw_tpl, raw_first, count, [8, 8, 8])]
    for cls, (url, num_regex) in zips.items():
        note = ("cell instance segmentation (uint32 ids; the 'plasma membrane' class)" if cls == "plasma_membrane"
                else "binary mask (0/255)")
        s = {"name": f"labels/{cls}", "type": "zip_slices", "zip_url": url, "first": 0, "count": count,
             "voxel_nm_zyx": [8, 8, 8], "note": note}
        if num_regex:
            s["num_regex"] = num_regex
        srcs.append(s)
    return {"name": f"{spec_id}_{label}_crop", "offset_zyx": offset, "size_zyx": [W, W, W],
            "voxel_nm_zyx": [8, 8, 8], "sources": srcs,
            "note": ("centre of the former 2000^3 window (labels cover the whole volume); labels are whole-volume binary masks (0/255), one array per class, "
                     "except plasma_membrane = cell instance ids. Obese1 er/LD/mito/nuclear-membrane slices are "
                     "10195x9110 vs raw 10200x9112 (top-left aligned; verified by overlay)")}


def build():
    D = []

    # ---------------------------------------------------------------- EMPIAR full downloads
    D.append({"id": "empiar-11037", "title": "CEM-MitoLab (2D mito instance labels)", "kind": "http_dir",
              "urls": [EMPIAR + "11037/"]})
    D.append({"id": "empiar-10982", "title": "MitoNet benchmarks (6 vEM volumes + 100 TEM)", "kind": "http_dir",
              "urls": [EMPIAR + "10982/"]})
    D.append({"id": "empiar-10994", "title": "HeLa SBF-SEM REEP3/4 (ER, mito)", "kind": "http_dir",
              "urls": [EMPIAR + "10994/"]})
    D.append({"id": "empiar-13156", "title": "HeLa STARD3 FIB-SEM", "kind": "http_dir", "urls": [EMPIAR + "13156/"]})
    D.append({"id": "empiar-11746", "title": "U2-OS FIB-SEM (EMPIAR part)", "kind": "http_dir",
              "urls": [EMPIAR + "11746/"]})
    D.append({"id": "zenodo-10043461", "title": "U2-OS FIB-SEM DeepMIB ground truth (Zenodo part of EMPIAR-11746)",
              "kind": "files", "files": zenodo(10043461)})
    D.append({"id": "empiar-12885", "title": "AIVE (subset: raw overviews, class labels, test ROI, human annotations)",
              "kind": "http_dir", "urls": [EMPIAR + "12885/"],
              "include": [r"^12885\.xml$",
                          r"^data/Set[1-3]_-_AIVE_SOURCE_DATA/Dataset[1-3]_overview(\.tif| - ORGANELLE CLASS LABELS\.tif| CLASS IDS\.txt)$",
                          r"^data/Set4_-_AIVE_SOURCE_DATA/Dataset4_(overview|MitoClasss)\.tif$",
                          r"^data/Set4_-_AIVE_OUTPUTS/",
                          r"^data/DATASET1_TEST_ROI/[^/]+\.tif$",
                          r"^data/DATASET1_TEST_ROI/Human_Vs_Unet_-_Mito_Classification/"],
              "note": "Skipped (per user decision): MembranePredictions, XYZCLAHE copies, Model_comparisons, duplicate folders."})

    # ---------------------------------------------------------------- other full downloads
    D.append({"id": "mitoem2", "title": "MitoEM 2.0 (Zenodo 17635006)", "kind": "files", "files": zenodo(17635006)})
    D.append({"id": "deepcontact", "title": "DeepContact training data (figshare 19898404)", "kind": "files",
              "files": figshare(19898404)})
    D.append({"id": "zenodo-15602048", "title": "TNBC TEM mitochondria (tem-seg)", "kind": "files",
              "files": zenodo(15602048)})
    D.append({"id": "zenodo-17068504", "title": "Cardiomyocyte TEM / MitoMapper", "kind": "files",
              "files": zenodo(17068504)})
    D.append({"id": "platynereis-zenodo", "title": "Platynereis CNN training data (Zenodo 3675220)", "kind": "files",
              "files": zenodo(3675220)})
    # fastem-4tu (4TU raw tiles, unlabeled) dropped 2026-10-12: labeled datasets only.
    D.append({"id": "platelet-guay", "title": "Guay et al. platelet SBF-SEM (dense-cell)", "kind": "files",
              "files": [{"url": "https://www.dropbox.com/s/68yclbraqq1diza/platelet_data_1219.zip?dl=1",
                         "dest": "platelet_data_1219.zip"}]})
    sb = "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/822/S-BIAD2822/Files/"
    sbiad = [("id_1", "2bf732c1", "tif", 201290072, 2442061), ("id_2", "97dde2ee", "tiff", 173445733, 2187643),
             ("id_3", "626dde70", "tif", 165713690, 1965077), ("id_4", "57f48663", "tif", 139858808, 3967367),
             ("id_5", "22668234", "tiff", 188860668, 6106748), ("id_6", "c69fbe8a", "tif", 292276384, 4300171),
             ("id_7", "4ff7ad90", "tif", 100000148, 1668167), ("id_8", "ee855e08", "tif", 171020286, 2573488),
             ("id_9", "387a5d97", "tif", 100001882, 1835737), ("id_10", "87356f94", "tif", 18087265, 321311),
             ("id_11", "4808a925", "tif", 183756636, 8897592), ("id_12", "596d5ecb", "tif", 178524596, 5828642)]
    files = []
    for i, h, ext, si, sl in sbiad:
        files.append({"url": f"{sb}{i}/img_{h}.{ext}", "dest": f"{i}/img_{h}.{ext}", "size": si})
        files.append({"url": f"{sb}{i}/lbl_{h}.tif", "dest": f"{i}/lbl_{h}.tif", "size": sl})
    D.append({"id": "s-biad2822", "title": "NucleoNet nuclei (BioImage Archive S-BIAD2822)", "kind": "files",
              "files": files})
    D.append({"id": "deeppi-em", "title": "DeepPI-EM skeletal muscle TEM (Google Drive)", "kind": "gdown_folder",
              "url": "https://drive.google.com/drive/folders/1LtnarR9R_zz0SPnEz5lSdYBQWNgYM1VT",
              "dest": "Skeletal_muscle", "expect_files": 54})
    D.append({"id": "orgsegnet", "title": "OrgSegNet / Plantorganelle Hunter (Science Data Bank)", "kind": "manual",
              "instructions": """
OrgSegNet / Plantorganelle Hunter dataset (32.4 GB, 6,076 files, CC-BY-4.0) is hosted on Science Data Bank,
which only serves downloads through its logged-in web UI, so it cannot be scripted.

1. Open https://doi.org/10.57760/sciencedb.01335
   (landing page: https://www.scidb.cn/en/detail?dataSetId=38ac871d518e41d5800a8abf39c26278)
2. Log in (free account) and use "Download all" (or download the folders individually).
3. Copy the files to this directory on the cluster (e.g. with rsync/scp via the data-transfer node).
Content: rice leaf TEM images (TIF, 4800x3730) + Labelme JSON + PNG masks for chloroplast, mitochondria,
vacuole, nucleus. 2D data, so no crops are needed.
"""})

    # ---------------------------------------------------------------- crops: OpenOrganelle
    D.append({"id": "openorganelle-crops",
              "title": "OpenOrganelle/COSEM: 1000^3 windows covering every GT crop of hela-2, macrophage-2, jurkat-1",
              "kind": "crops",
              "crops": cosem_crops("jrc_hela-2") + cosem_crops("jrc_macrophage-2") + cosem_crops("jrc_jurkat-1")})

    # ---------------------------------------------------------------- crops: EMPIAR-13420 (37.9 GB)
    e = EMPIAR + "13420/data/"
    D.append({"id": "empiar-13420-crops", "title": "EMPIAR-13420 macrophage/A431: 3 crops (2 FIB-SEM, 1 SBF-SEM)",
              "kind": "crops", "extra_files": [{"url": e + "README", "dest": "README"}], "crops": [
                  {"name": "FIB_240729_HumanMP_TNFa_1U_crop", "offset_zyx": [225, 0, 950], "size_zyx": [W, W, W],
                   "voxel_nm_zyx": [12, 6, 6], "labels_note": "1 nucleus, 2 lipid droplets",
                   "sources": [
                       tiff_stack("raw", e + "FIB/240729_HumanMP_TNFa_1U/dataset/TIFs/240729_HumanMP_TNFa_1U_{:04d}.tif", 1, 2719, [12, 6, 6]),
                       tiff_stack("labels/nucleus_ld", e + "FIB/240729_HumanMP_TNFa_1U/labels/Labels_NucleusLDs_TIFs/Labels_240729_HumanMP_TNFa_1U_nucleus_LD_{:04d}.tif", 1, 2719, [12, 6, 6])]},
                  {"name": "FIB_240801_HumanMP_Control_crop", "offset_zyx": [1370, 0, 1850], "size_zyx": [W, W, W],
                   "voxel_nm_zyx": [12, 6, 6],
                   "labels_note": "1 mitochondria, 2 Golgi, 3 ER, 4 nucleus, 5 lipid droplets, 6 ER of other cells",
                   "sources": [
                       tiff_stack("raw", e + "FIB/240801_HumanMP_Control/dataset/TIFs/240801_HumanMP_Control_{:04d}.tif", 1, 2888, [12, 6, 6]),
                       tiff_stack("labels/combined", e + "FIB/240801_HumanMP_Control/labels/Labels_Combined_model_TIFs/Labels_240801_HumanMP_Control_combined_{:04d}.tif", 1, 2888, [12, 6, 6])]},
                  {"name": "SBF_240424_Microphages_no_load_crop", "offset_zyx": [0, 4350, 2000], "size_zyx": [W, W, W],
                   "voxel_nm_zyx": [30, 10, 10], "labels_note": "1 nuclei, 2 LDs below nuclei, 3 LDs above nuclei",
                   "sources": [
                       tiff_stack("raw", e + "SBFSEM/240424_Microphages_no_load/dataset/TIFs/240424_Microphages_no_load_R02_{:03d}.tif", 1, 257, [30, 10, 10]),
                       tiff_stack("labels/nuclei_ld", e + "SBFSEM/240424_Microphages_no_load/labels/Labels_TIF/Labels_240424_Microphages_no_load_R02_Nuclei_{:03d}.tif", 1, 257, [30, 10, 10])]},
              ]})

    # ---------------------------------------------------------------- crops: EMPIAR-10791 liver (2.8 TB)
    def zips(prefix, names):
        return {k: (L10791 + prefix + v, rx) for k, (v, rx) in names.items()}
    lean = zips("6461%20-%20Lean%20Liver/6461%20Lean%20Liver%20-%20Segmentation/6461%20Lean%20", {
        "er": ("ER.zip", None), "er_sheets": ("ER%20Sheets.zip", None), "er_tubules": ("ER%20Tubules.zip", None),
        "lipid_droplet": ("Lipid%20Droplet.zip", None), "mitochondria": ("Mitochondria.zip", None),
        "nuclear_membrane": ("Nuclear%20membrane.zip", None), "plasma_membrane": ("Plasma%20Membrane.zip", None)})
    ob1 = zips("6464%20-%20Obese1%20Liver/6464%20Obese1%20Liver%20-%20Segmentation/6464%20Obese1%20", {
        "er": ("ER.zip", None), "er_sheets": ("ER%20Sheets.zip", r"_Z(\d+)_C"), "er_tubules": ("ER%20Tubules.zip", r"_Z(\d+)_C"),
        "lipid_droplet": ("Lipid%20Droplet.zip", None), "mitochondria": ("Mitochondria.zip", None),
        "nuclear_membrane": ("Nuclear%20membrane.zip", None), "plasma_membrane": ("Plasma%20membrane.zip", None)})
    ob2 = zips("9430%20-%20Obese2%20Liver/9430%20Obese2%20Liver%20-%20Segmentation/9430%20Obese2%20", {
        "er": ("ER.zip", None), "er_sheets": ("ER%20Sheets.zip", None), "er_tubules": ("ER%20Tubules.zip", None),
        "lipid_droplet": ("Lipid%20Droplet.zip", None), "mitochondria": ("Mitochondria.zip", None),
        "nuclear_membrane": ("nuclear%20membrane.zip", None), "plasma_membrane": ("Plasma%20membrane.zip", None)})
    crops = [
        liver("6461", "lean", L10791 + "6461%20-%20Lean%20Liver/6461%20Lean%20Liver%20-%20Raw/Gunes_WT1_8x8x8nm_3MHz.{:04d}.tif",
              0, 5638, lean, [2319, 3500, 5500]),
        liver("6464", "obese1", L10791 + "6464%20-%20Obese1%20Liver/6464%20Obese1%20Liver%20-%20Raw%20reupload/6464%20Ob%20Fed%20Raw%20with%20All_T001_Z{:03d}_C09.tif",
              1, 7896, ob1, [3448, 4600, 4056]),
        liver("9430", "obese2", L10791 + "9430%20-%20Obese2%20Liver/9430%20Obese2%20Liver%20-%20Raw/9430%20raw/9430%20Ob%20Fed%20Raw_T001_Z{:03d}_C07.tif",
              1, 8501, ob2, [3750, 3525, 3500]),
    ]
    # ER sheets/tubules member names of 6464 encode z as _Z001_ (1-based)
    for s in crops[1]["sources"]:
        if s.get("num_regex"):
            s["first"] = 1
    D.append({"id": "empiar-10791-crops", "title": "EMPIAR-10791 mouse liver: 1 crop per specimen (lean, obese1, obese2)",
              "kind": "crops", "crops": crops,
              "extra_files": [{"url": L10791 + "READ_ME_PLEASE.txt", "dest": "READ_ME_PLEASE.txt"},
                              {"url": L10791 + "Segmentation%20Evaluation/Manual%20annotation%20-%20Prediction%20-%20Raw%20Slices.zip",
                               "dest": "Segmentation_Evaluation/Manual annotation - Prediction - Raw Slices.zip"}]})

    # ---------------------------------------------------------------- crops: ASEM
    def asem_labeled_chunks(cell, cls):
        """Chunk boxes [(start_zyx, size_zyx)] of a label array that hold labels. Every chunk is stored, so an
        all-zero chunk is recognised by its size (the smallest object of the array)."""
        pre = f"datasets/{cell}/{cell}.zarr/volumes/labels/{cls}/"
        za = S.get(f"{ASEM}{cell}/{cell}.zarr/volumes/labels/{cls}/.zarray").json()
        keys, _ = s3_list("asem-project", pre)
        ch = [(list(map(int, re.split(r"[./]", k[len(pre):]))), n) for k, n in keys
              if re.fullmatch(r"\d+([./]\d+){2}", k[len(pre):])]
        empty = min(n for _, n in ch)
        c = za["chunks"]
        return za["shape"], [([i * w for i, w in zip(idx, c)], c) for idx, n in ch if n > empty]

    def asem_cell(cell, labels, raw_off_vox, small=()):
        base = f"{ASEM}{cell}/{cell}.zarr/volumes/"
        lab = {l: asem_labeled_chunks(cell, l) for l in labels}
        shape = next(iter(lab.values()))[0]
        allch = [b for _, bs in lab.values() for b in bs]
        spans = []  # per axis: one W span if all labels fit in it, else equal tiles <= W
        for a in range(3):
            lo = min(st[a] for st, _ in allch)
            hi = min(shape[a], max(st[a] + sz[a] for st, sz in allch))
            if hi - lo <= W or shape[a] <= W:
                st = max(0, min(lo, shape[a] - W))
                spans.append([(st, min(W, shape[a]))])
            else:
                spans.append(tiles(shape[a]))

        def hits(t, bs):
            return any(all(st[a] < t[a][0] + t[a][1] and t[a][0] < st[a] + sz[a] for a in range(3)) for st, sz in bs)
        crops = []
        for t in itertools.product(*spans):
            have = [l for l in labels if hits(t, lab[l][1])]
            if not have:
                continue
            srcs = [{"name": "raw", "type": "zarr2", "url": base + "raw", "voxel_nm_zyx": [5, 5, 5],
                     "world_offset_vox_zyx": raw_off_vox}]
            srcs += [{"name": f"labels/{l}", "type": "zarr2", "url": base + f"labels/{l}", "voxel_nm_zyx": [5, 5, 5],
                      "world_offset_vox_zyx": raw_off_vox,
                      "note": "instance labels, sparse annotation (0 = not annotated, not necessarily background)"}
                     for l in have]
            crops.append({"name": f"cell_{cell}_t{len(crops) + 1:02d}", "offset_zyx": [a for a, _ in t],
                          "size_zyx": [n for _, n in t], "voxel_nm_zyx": [5, 5, 5], "sources": srcs,
                          "labels_present": have,
                          "note": f"tile of cell {cell} holding labeled chunks of {have}"})
        for l, woff, bshape in small:  # small annotated blocks: one W^3 window centred on the block
            rel = [w - r for w, r in zip(woff, raw_off_vox)]  # block start in raw array coordinates
            rshape = S.get(base + "raw/.zarray").json()["shape"]
            off = [max(0, min(n - min(W, n), s + b // 2 - W // 2)) for s, b, n in zip(rel, bshape, rshape)]
            crops.append({"name": f"cell_{cell}_{l}_block", "offset_zyx": off, "size_zyx": [W, W, W],
                          "voxel_nm_zyx": [5, 5, 5], "labels_present": [l],
                          "sources": [{"name": "raw", "type": "zarr2", "url": base + "raw", "voxel_nm_zyx": [5, 5, 5],
                                       "world_offset_vox_zyx": raw_off_vox},
                                      {"name": f"labels/{l}", "type": "zarr2", "url": base + f"labels/{l}",
                                       "voxel_nm_zyx": [5, 5, 5], "offset_zyx": [0, 0, 0], "size_zyx": BIG,
                                       "world_offset_vox_zyx": woff,
                                       "note": "small annotated block; position given by world_offset (same world grid as raw)"}],
                          "note": f"window centred on the small {l} annotation block of cell {cell}"})
        return crops
    D.append({"id": "asem-crops", "title": "ASEM / incasem FIB-SEM: tiles over all labeled regions (cells 46, 58, 61)",
              "kind": "crops",
              "extra_files": [{"url": "https://asem-project.s3.amazonaws.com/README.md", "dest": "README.md"},
                              {"url": "https://asem-project.s3.amazonaws.com/datasets_info.xlsx", "dest": "datasets_info.xlsx"}],
              "crops": (asem_cell("46", ["mito", "er", "golgi"], [0, 0, 0]) +
                        asem_cell("58", ["mito", "er", "golgi"], [0, 0, 0]) +
                        asem_cell("61", ["mito"], [0, 1750, 2900],
                                  small=[("er", [1351, 2281, 6748], [204, 204, 400]),
                                         ("golgi", [1000, 2160, 5497], [210, 283, 371])]))})

    # ---------------------------------------------------------------- crops: FAST-EM WebKnossos
    def fastem(i, y, x):
        def wk(name, layer, note):
            return {"name": name, "type": "zarr2", "axes": "cxyz", "url": f"{WK}{layer}/1-1-1/",
                    "voxel_nm_zyx": [100, 4, 4], "note": note}
        return {"name": f"MCF7_UAc_crop{i}", "offset_zyx": [0, y + 500, x + 500], "size_zyx": [W, W, W],
                "voxel_nm_zyx": [100, 4, 4], "note": "inside the Mito_GT bounding box; 54 sections thick",
                "sources": [wk("raw", "EM", "EM layer as acquired"),
                            wk("labels/mito_gt", "Mito_GT_EM", "mito instance GT on 'raw'"),
                            wk("raw_realigned", "GT_EM_realigned_SOFIMA", "SOFIMA-realigned EM of the GT region"),
                            wk("labels/mito_gt_realigned", "Mito_GT_EM_realigned_SOFIMA", "mito instance GT on 'raw_realigned'")]}
    D.append({"id": "fastem-crops", "title": "FAST-EM MCF-7 WebKnossos volume: 3 crops with mito GT", "kind": "crops",
              "crops": [fastem(1, 52496, 27312), fastem(2, 58112, 25776), fastem(3, 54064, 25312)]})

    # Platynereis raw crops (unlabeled) dropped 2026-10-12: labeled datasets only.
    return {"generated": "2026-10-12", "crop_edge": W, "datasets": D}


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "datasets.json")
    m = build()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    print(f"wrote {out}: {len(m['datasets'])} datasets")
