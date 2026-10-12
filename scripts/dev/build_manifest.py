#!/usr/bin/env python3
"""Regenerate ../datasets.json. Developer tool: queries Zenodo/figshare/4TU/S3 for current file lists + md5s.

Crop positions were chosen by analysis on 2026-10-08 (see README.md, "How crop positions were chosen") and are
hard-coded here so the manifest is deterministic.
"""
import json
import os
import requests

S = requests.Session()
EMPIAR = "https://ftp.ebi.ac.uk/empiar/world_availability/"
COSEM = "https://janelia-cosem-datasets.s3.amazonaws.com/"
ASEM = "https://asem-project.s3.amazonaws.com/datasets/"
WK = "https://webknossos.tnw.tudelft.nl/data/zarr/6672e7da010000f100a1be53/"
PLATY = "https://buckets.embl.de/platybrowser-n5-generic/platybrowser/rawdata/sbem-6dpf-1-whole-raw.n5/setup0/timepoint0/s0"
L10791 = (EMPIAR + "10791/data/Parlakgul%20et%20al%20-%20Regulation%20of%20liver%20subcellular%20architecture%20"
          "controls%20metabolic%20homeostasis/FIB-SEM%20Raw%20and%20Segmentation%20Data/")
BIG = [10 ** 6] * 3  # "whole array" (clipped to the source extent)


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


def cosem_crop(ds, offset, size, gt_crops):
    base = f"{COSEM}{ds}/{ds}.zarr/recon-1/"
    em = S.get(base + "em/fibsem-uint8/.zattrs").json()["multiscales"][0]["datasets"]
    vox = [d for d in em if d["path"] == "s0"][0]["coordinateTransformations"][0]["scale"]
    srcs = [{"name": "raw", "type": "zarr2", "url": base + "em/fibsem-uint8/s0", "voxel_nm_zyx": vox}]
    for c in gt_crops:
        ms = S.get(f"{base}labels/groundtruth/{c}/all/.zattrs").json()["multiscales"][0]["datasets"]
        ct = {d["path"]: d["coordinateTransformations"] for d in ms}
        for lvl, tag in (("s1", ""), ("s0", "_s0")):
            sc, tr = ct[lvl][0]["scale"], ct[lvl][1]["translation"]
            srcs.append({"name": f"labels/gt_{c}{tag}", "type": "zarr2", "url": f"{base}labels/groundtruth/{c}/all/{lvl}",
                         "offset_zyx": [0, 0, 0], "size_zyx": BIG, "voxel_nm_zyx": sc,
                         "world_offset_vox_zyx": [round(t / s, 3) for t, s in zip(tr, sc)],
                         "note": ("dense multi-class GT ('all'), aligned with raw (EM s0 grid)" if lvl == "s1" else
                                  "dense multi-class GT at native annotation resolution (2x finer than raw)")})
    return {"name": f"{ds}_crop", "offset_zyx": offset, "size_zyx": size, "voxel_nm_zyx": vox, "sources": srcs,
            "note": f"2000^3 window chosen to contain GT crops {gt_crops} completely"}


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
    return {"name": f"{spec_id}_{label}_crop", "offset_zyx": offset, "size_zyx": [2000, 2000, 2000],
            "voxel_nm_zyx": [8, 8, 8], "sources": srcs,
            "note": ("centred in the volume; labels are whole-volume binary masks (0/255), one array per class, "
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
    D.append({"id": "fastem-4tu", "title": "FAST-EM MCF-7 sample (4TU, raw tiles, unlabeled)", "kind": "files",
              "files": fourtu("bf3f2b23-2328-4d81-a0f4-05fdb33117d7")})
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
    D.append({"id": "openorganelle-crops", "title": "OpenOrganelle/COSEM: 3 crops (hela-2, macrophage-2, jurkat-1) + GT",
              "kind": "crops", "crops": [
                  cosem_crop("jrc_hela-2", [1249, 0, 2140], [2000, 2000, 2000],
                             ["crop113", "crop23", "crop6", "crop8", "crop9", "crop96"]),
                  cosem_crop("jrc_macrophage-2", [4011, 0, 1340], [2000, 2000, 2000],
                             ["crop110", "crop40", "crop73", "crop89"]),
                  cosem_crop("jrc_jurkat-1", [2300, 0, 4493], [2000, 2000, 2000],
                             ["crop107", "crop126", "crop180", "crop182", "crop71", "crop91"])]})

    # ---------------------------------------------------------------- crops: EMPIAR-13420 (37.9 GB)
    e = EMPIAR + "13420/data/"
    D.append({"id": "empiar-13420-crops", "title": "EMPIAR-13420 macrophage/A431: 3 crops (2 FIB-SEM, 1 SBF-SEM)",
              "kind": "crops", "extra_files": [{"url": e + "README", "dest": "README"}], "crops": [
                  {"name": "FIB_240729_HumanMP_TNFa_1U_crop", "offset_zyx": [0, 0, 550], "size_zyx": [2000, 2000, 2000],
                   "voxel_nm_zyx": [12, 6, 6], "labels_note": "1 nucleus, 2 lipid droplets",
                   "sources": [
                       tiff_stack("raw", e + "FIB/240729_HumanMP_TNFa_1U/dataset/TIFs/240729_HumanMP_TNFa_1U_{:04d}.tif", 1, 2719, [12, 6, 6]),
                       tiff_stack("labels/nucleus_ld", e + "FIB/240729_HumanMP_TNFa_1U/labels/Labels_NucleusLDs_TIFs/Labels_240729_HumanMP_TNFa_1U_nucleus_LD_{:04d}.tif", 1, 2719, [12, 6, 6])]},
                  {"name": "FIB_240801_HumanMP_Control_crop", "offset_zyx": [800, 0, 1300], "size_zyx": [2000, 2000, 2000],
                   "voxel_nm_zyx": [12, 6, 6],
                   "labels_note": "1 mitochondria, 2 Golgi, 3 ER, 4 nucleus, 5 lipid droplets, 6 ER of other cells",
                   "sources": [
                       tiff_stack("raw", e + "FIB/240801_HumanMP_Control/dataset/TIFs/240801_HumanMP_Control_{:04d}.tif", 1, 2888, [12, 6, 6]),
                       tiff_stack("labels/combined", e + "FIB/240801_HumanMP_Control/labels/Labels_Combined_model_TIFs/Labels_240801_HumanMP_Control_combined_{:04d}.tif", 1, 2888, [12, 6, 6])]},
                  {"name": "SBF_240424_Microphages_no_load_crop", "offset_zyx": [0, 2600, 1900], "size_zyx": [2000, 2000, 2000],
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
              0, 5638, lean, [1819, 3000, 5000]),
        liver("6464", "obese1", L10791 + "6464%20-%20Obese1%20Liver/6464%20Obese1%20Liver%20-%20Raw%20reupload/6464%20Ob%20Fed%20Raw%20with%20All_T001_Z{:03d}_C09.tif",
              1, 7896, ob1, [2948, 4100, 3556]),
        liver("9430", "obese2", L10791 + "9430%20-%20Obese2%20Liver/9430%20Obese2%20Liver%20-%20Raw/9430%20raw/9430%20Ob%20Fed%20Raw_T001_Z{:03d}_C07.tif",
              1, 8501, ob2, [3250, 3025, 3000]),
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
    def asem(cell, offset, labels, raw_off_vox, small=()):
        base = f"{ASEM}{cell}/{cell}.zarr/volumes/"
        srcs = [{"name": "raw", "type": "zarr2", "url": base + "raw", "voxel_nm_zyx": [5, 5, 5],
                 "world_offset_vox_zyx": raw_off_vox}]
        for l in labels:
            srcs.append({"name": f"labels/{l}", "type": "zarr2", "url": base + f"labels/{l}", "voxel_nm_zyx": [5, 5, 5],
                         "world_offset_vox_zyx": raw_off_vox, "note": "instance labels, full-size array (sparse)"})
        for l, woff in small:
            srcs.append({"name": f"labels/{l}", "type": "zarr2", "url": base + f"labels/{l}", "voxel_nm_zyx": [5, 5, 5],
                         "offset_zyx": [0, 0, 0], "size_zyx": BIG, "world_offset_vox_zyx": woff,
                         "note": "small annotated block; position given by world_offset (same world grid as raw)"})
        return {"name": f"cell_{cell}_crop", "offset_zyx": offset, "size_zyx": [2000, 2000, 2000],
                "voxel_nm_zyx": [5, 5, 5], "sources": srcs,
                "note": "window placed over the annotated region (from non-empty label chunks)"}
    D.append({"id": "asem-crops", "title": "ASEM / incasem FIB-SEM: 3 crops (cells 46, 58, 61) + labels",
              "kind": "crops",
              "extra_files": [{"url": "https://asem-project.s3.amazonaws.com/README.md", "dest": "README.md"},
                              {"url": "https://asem-project.s3.amazonaws.com/datasets_info.xlsx", "dest": "datasets_info.xlsx"}],
              "crops": [asem("46", [0, 0, 1000], ["mito", "er", "golgi"], [0, 0, 0]),
                        asem("58", [0, 0, 800], ["mito", "er", "golgi"], [0, 0, 0]),
                        asem("61", [0, 0, 2300], ["mito"], [0, 1750, 2900],
                             small=[("er", [1351, 2281, 6748]), ("golgi", [1000, 2160, 5497])])]})

    # ---------------------------------------------------------------- crops: FAST-EM WebKnossos
    def fastem(i, y, x):
        def wk(name, layer, note):
            return {"name": name, "type": "zarr2", "axes": "cxyz", "url": f"{WK}{layer}/1-1-1/",
                    "voxel_nm_zyx": [100, 4, 4], "note": note}
        return {"name": f"MCF7_UAc_crop{i}", "offset_zyx": [0, y, x], "size_zyx": [2000, 2000, 2000],
                "voxel_nm_zyx": [100, 4, 4], "note": "inside the Mito_GT bounding box; 54 sections thick",
                "sources": [wk("raw", "EM", "EM layer as acquired"),
                            wk("labels/mito_gt", "Mito_GT_EM", "mito instance GT on 'raw'"),
                            wk("raw_realigned", "GT_EM_realigned_SOFIMA", "SOFIMA-realigned EM of the GT region"),
                            wk("labels/mito_gt_realigned", "Mito_GT_EM_realigned_SOFIMA", "mito instance GT on 'raw_realigned'")]}
    D.append({"id": "fastem-crops", "title": "FAST-EM MCF-7 WebKnossos volume: 3 crops with mito GT", "kind": "crops",
              "crops": [fastem(1, 52496, 27312), fastem(2, 58112, 25776), fastem(3, 54064, 25312)]})

    # ---------------------------------------------------------------- crops: Platynereis raw
    D.append({"id": "platynereis-crops", "title": "Platynereis 6dpf whole-body SBEM raw: 3 crops (unlabeled)",
              "kind": "crops", "crops": [
                  {"name": f"sbem-6dpf-1_crop{i}", "offset_zyx": o, "size_zyx": [2000, 2000, 2000],
                   "voxel_nm_zyx": [25, 10, 10], "note": "placed inside the animal (tissue mask from s6)",
                   "sources": [{"name": "raw", "type": "n5", "url": PLATY, "voxel_nm_zyx": [25, 10, 10]}]}
                  for i, o in enumerate([[2112, 14016, 19264], [7040, 14464, 13248], [8448, 12032, 10688]], 1)]})
    return {"generated": "2026-10-08", "datasets": D}


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "datasets.json")
    m = build()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    print(f"wrote {out}: {len(m['datasets'])} datasets")
