# EM organelle datasets: download / crop scripts

Downloads the **labeled** public datasets listed in `../sources.csv` (catalogued in `../metadata.csv`) onto the
cluster. Scope since 2026-10-12: labeled data only (the unlabeled Platynereis raw crops and the unlabeled FAST-EM
4TU tiles are no longer downloaded).

* Datasets **≤ 30 GB** are downloaded in full, with md5 verification where the repository publishes one.
* Datasets **> 30 GB** are cut into crops of **at most 1000×1000×1000 voxels** placed over the labels (see "How
  crop positions were chosen"). A crop is clipped where the volume is thinner, e.g. FAST-EM is only 54 sections.
  Crops are written as **OME-Zarr** (v0.4, zarr format 2, Blosc-zstd, chunks 64×256×256), with labels in the
  same file, plus a **JSON sidecar** per crop (source URLs, offsets, voxel size, dtype, missing slices).
* `inventory.py` writes a CSV with one row per downloaded item and all its statistics (shape, voxel size and
  where it came from, physical size, dtype, label classes/objects/fraction, intensity range). See "Inventory".

## Files

| file | purpose |
|---|---|
| `fetch_em.py` | the downloader (one file, no project imports) |
| `datasets.json` | manifest: one entry per job, i.e. per dataset or per dataset part |
| `download_array.sbatch` | SLURM job array, one task per manifest entry |
| `inventory.py`, `inventory.sbatch` | CSV of every downloaded item with its statistics (run after the download) |
| `verify.py`, `verify.sbatch`, `verify_one.sbatch` | checks every download (or one: `verify_one.sbatch <idx>`) is complete, measures sizes, writes `_dataset_info.json` per dataset |
| `setup_env.sh`, `requirements.txt` | creates `./venv` |
| `dev/build_manifest.py` | regenerates `datasets.json` (queries Zenodo/figshare/4TU for md5s) |
| `dev/build_metadata.py` | regenerates `../metadata.csv` |

## Installation (one time)

Everything installs into a private Python virtual environment inside this folder (`./venv`). It needs no
admin rights and does not touch the cluster's system Python or anyone else's environment.

### What you need
* A Linux login on the cluster with SLURM (`sbatch`, `squeue`, `sacct`).
* **Python ≥ 3.9.** Python ≥ 3.11 is preferred, because it gets zarr 3; older versions get zarr 2, which also
  works.
* Internet access from the node where you install. The compute nodes need it too when the jobs run (they have
  it, and so does the data-transfer node).
* About 200 MB of disk for `./venv`. The **data** needs far more; see `../DATASET_SIZES.md`.

### Step 1: copy the folder to the cluster
From your own computer (replace user/host/path):
```bash
rsync -av scripts/ myuser@cluster.example.org:~/em_fetch/
# or:  scp -r scripts myuser@cluster.example.org:~/em_fetch
```
Copy the whole folder: `fetch_em.py`, `datasets.json`, `download_array.sbatch`, `inventory.py`,
`inventory.sbatch`, `setup_env.sh`, `requirements.txt` (and `dev/`, which is optional). Then log in and
`cd ~/em_fetch`. Or clone the repository: `git clone https://github.com/HashemW/em-labeled-datasets.git` (public, no login
needed) and `cd em-labeled-datasets/scripts`.

If you copied from Windows and a script fails with `$'\r': command not found`, fix the line endings with
`sed -i 's/\r$//' *.sh *.sbatch`. The files are shipped with Unix line endings, so this should not happen.

### Step 2: get a suitable Python
Check what you have:
```bash
python3 --version
```
If it is older than 3.9, or you want 3.11+, load one. Pick whichever your cluster offers:
```bash
module avail python             # list Python modules
module load python/3.11         # name/version differs per cluster
# or, with conda/mamba:
conda create -y -p ~/em_fetch/pyenv python=3.11 && conda activate ~/em_fetch/pyenv
```
Run Step 3 on a node with internet: the login node or the data-transfer node, or interactively with
`srun --pty bash` on a compute node.

### Step 3: create the environment
```bash
cd ~/em_fetch
bash setup_env.sh               # creates ./venv and installs requirements.txt
```
To put the environment elsewhere, e.g. because home is small: `bash setup_env.sh /path/to/venv`, then set
`ENV_DIR=/path/to/venv` when submitting jobs (Step 6). To use a specific interpreter:
`PYTHON=/path/to/python3 bash setup_env.sh`.

The script ends with `environment OK` and the zarr version.

**If `pip install` fails:**
* *Behind an HTTP proxy:* `export HTTPS_PROXY=http://proxy.example.org:3128` (ask IT for the address), then
  re-run. The downloader also honours `HTTPS_PROXY`.
* *No compiler / building `imagecodecs` from source:* upgrade pip first (`./venv/bin/python -m pip install -U
  pip`), because recent pip finds prebuilt wheels. If it still builds from source, use Python 3.10–3.12, which
  have wheels.
* *Module Python missing `venv`:* use the conda route from Step 2.

### Step 4: check the installation
```bash
./venv/bin/python fetch_em.py --list          # should print 21 numbered datasets (0-20)
```
Then run the **smoke test**. It touches every download route with tiny reads (64³ crops, the first MiB of big
files, folder listings), writes well under 1 GB, and takes 15–30 minutes:
```bash
./venv/bin/python fetch_em.py --out /scratch/$USER/em_test --test
```
It ends with a summary table. Every line should be `OK`; `orgsegnet` shows `OK (MANUAL)`. Use a **different**
`--out` for the test than for the real run; the script refuses to let a test overwrite real crops.
Delete the test folder afterwards.

If the login node kills long processes, run the smoke test as a job instead:
`sbatch --wrap "./venv/bin/python fetch_em.py --out /scratch/$USER/em_test --test" --time=1:00:00 --mem=8G`

### Step 5: configure the job script
Edit the header of `download_array.sbatch`:
* `--partition`: set to `short` (our cluster). Add `--account` if your cluster requires one. If there is a
  data-transfer partition, use it.
* `--time`: 24 hours. Tasks are resumable, so a task that hits the limit (most likely 16, the 49 OpenOrganelle
  windows, and 18, the EMPIAR-10791 liver crops) is simply re-submitted and continues where it stopped.
* `--array=0-20%4`: `%4` = at most 4 downloads at once. That is polite to the servers; raise it if you like.
* `--mem=32G`, `--cpus-per-task=8`: enough for all tasks.

### Step 6: choose the output location and submit
Choose a filesystem with enough **quota** for everything you will keep. The plan stores **≈ 125 GB**
(+32 GB if you add OrgSegNet by hand; table below), and leave ~20–30 % headroom. Project or scratch space is usually right; home usually is not. Check free space
with `df -h /path` or your site's quota command.
```bash
cd ~/em_fetch
mkdir -p logs
sbatch download_array.sbatch                              # OUT defaults to ../../em_datasets (next to the repo)
OUT=/project/mylab/em_datasets sbatch download_array.sbatch   # or choose another place
# if the venv is not in ./venv:
OUT=/project/mylab/em_datasets ENV_DIR=/path/to/venv sbatch download_array.sbatch
```

## Running and monitoring

```bash
squeue -u $USER                                   # running/pending tasks
tail -f logs/em-fetch_<jobid>_<task>.out          # live log of one task
sacct -j <jobid> --format=JobID,JobName%20,State,Elapsed,ExitCode    # results per task
cat $OUT/<dataset-id>/_status.json                # what was downloaded / what failed
```
A task that ends `FAILED` in `sacct` has the reason in its log and in `_status.json` (`"failed": [...]`).
Most failures are temporary server problems. **Re-submit just those tasks**; everything already done is
skipped and partial files and crops resume:
```bash
sbatch --array=14,18 download_array.sbatch      # add the same OUT=... if you set one at first submit
```
To run a single dataset without SLURM (e.g. on the transfer node):
`./venv/bin/python fetch_em.py --out $OUT --only empiar-12885`

After a successful run, the one manual dataset (OrgSegNet) is described in `$OUT/orgsegnet/MANUAL_DOWNLOAD.txt`.

## Verify + per-dataset metadata JSON

After all download tasks are `COMPLETED`:
```bash
./venv/bin/python verify.py --out $OUT          # quick check (minutes): completeness + sizes + JSONs
sbatch verify.sbatch                            # full run: inventory.py (items.csv), then verify.py --deep
```
`verify.sbatch` uses the same `OUT` default as `download_array.sbatch` (`OUT=... sbatch verify.sbatch` for another
place). It runs `inventory.py` first, so the JSONs carry values measured on the files, then `verify.py --deep`.

What is checked: `_status.json` says OK and is not from a test run; no leftover `.part`/`.badmd5` files; manifest
files have the exact published size (`--deep`: and md5); EMPIAR/EBI folders are listed again and every file must
exist with the exact size the server reports; Google Drive has the expected number of non-empty files; every crop
has its sidecar, all arrays, the expected shape and all z-slabs written (`--deep`: every voxel is read back and
all-zero arrays are flagged); `--deep` also CRC-tests every zip. `--offline` skips the server comparison.

Outputs:
* `$OUT/verify_report.csv` (+ `.json`): one row per dataset with status `OK` / `WARN` / `FAIL` / `MANUAL`, GB on
  disk, disk usage (quota), the exact expected GB where the server publishes sizes, and the problems found.
* `$OUT/<id>/_dataset_info.json`: everything about the dataset in one file:
  * `metadata`: name, used for, dataset link, paper link, imaging, organism, organ/tissue/cell type, organelles,
    label classes, xy/z resolution, image size, size at source, download plan, notes (from `../metadata.csv`).
  * `on_disk`: bytes, GB, file count, disk usage, expected GB.
  * `verification`: status, problems, warnings, what was checked.
  * `measured_summary`: the distinct voxel sizes (z/y/x nm, with their source), the image-size range in pixels,
    and the label classes found.
  * `items`: one entry per item from `items.csv` (shape, voxel size, physical size, dtype, label statistics,
    intensity range, ...). Without `items.csv` it falls back to `../planned_items.csv` (headers read before download).
  * `crops` (crop datasets): per crop the offset, voxel size and every array with shape, dtype, source URL and the
    OpenOrganelle GT crops it holds.

A `FAIL` names what is missing; re-submit that task (`sbatch --array=<idx> download_array.sbatch`), then check only
that dataset with the same full checks:
```bash
sbatch verify_one.sbatch <idx>                  # task number, e.g. 17, or the dataset id; several: 3 17
```
It replaces only that dataset's rows in `items.csv` and `verify_report.csv` and keeps all other rows (the same as
`verify.py --only <id>` and `inventory.py --only <id>`).

## Inventory (the "exactly what we have" CSV)

After all tasks are `COMPLETED`, run:
```bash
OUT=/project/mylab/em_datasets sbatch inventory.sbatch      # writes $OUT/items.csv (hours: reads everything)
```
or directly: `./venv/bin/python inventory.py --out $OUT --csv $OUT/items.csv` (`--only <ids>` for a subset).
One row per item: a crop array, a 3D file (NIfTI, multi-page TIFF, HDF5, N5, Amira), a numbered TIFF slice series
(one volume), a folder of 2D images, or a folder of Labelme polygon files. Columns include the region taken
(crop offset or slice range), shape z/y/x (min–max for image sets), dtype, voxel size z/y/x in nm with
`voxel_source` (file header, repository metadata, or paper) and the raw `voxel_in_header`, physical size in µm,
bytes on disk, label classes / number of label values / number of objects / labeled fraction, intensity
min/max/mean/std, imaging, organism, cell type, and notes (e.g. label shape differs from raw, missing slices).
`n_objects` = number of distinct nonzero label values: the instance count for instance labels (e.g. MitoEM),
the class count for semantic masks (a binary mask gives 1), and the polygon count for Labelme. Connected
components are not counted. `labeled_fraction` = share of voxels that are nonzero. `role` is raw / label /
prediction / weights (training weight maps) / other.

`../planned_items.csv` is the same table made **before** downloading (`inventory.py --remote`): it reads file
headers over HTTP, samples large 2D image sets, and leaves label/intensity statistics empty.

### Troubleshooting
| symptom | cause / fix |
|---|---|
| `deeppi-em` fails mentioning quota/permission | Google Drive rate limit. Re-submit task 14 after a few hours. |
| log shows `FTP cross-check unavailable` | Port 21 is blocked on the node. Harmless; HTTPS gap-probing still runs. |
| log shows `integrity: ... differed between reads` | A corrupted transfer was caught and replaced. Nothing to do. |
| `gap in numbered sequence (file really absent)` | The file does not exist on the server (e.g. EMPIAR-13156 `ROI_CICGFA_154.tif`). Nothing to do. |
| job hits the time limit | Re-submit the same task; it resumes. |
| `refusing to overwrite it with a test crop` | `--test` was pointed at a real output folder. Use another `--out`. |

`python fetch_em.py --list` prints the task index → dataset mapping:

| idx | id | what | stored on disk | network transfer |
|---|---|---|---:|---:|
| 0 | empiar-11037 | CEM-MitoLab, full | 2.8 GB | 2.8 GB |
| 1 | empiar-10982 | MitoNet benchmarks, full | 4.1 GB | ≈ 8.2 GB |
| 2 | empiar-10994 | HeLa SBF-SEM, full | 8.7 GB | ≈ 17.4 GB |
| 3 | empiar-13156 | HeLa STARD3 FIB-SEM, full | 10.3 GB | ≈ 20.6 GB |
| 4 | empiar-11746 | U2-OS FIB-SEM, full | 5.2 GB | ≈ 10.4 GB |
| 5 | zenodo-10043461 | U2-OS DeepMIB GT, full | 4.9 GB | 4.9 GB |
| 6 | empiar-12885 | AIVE, useful subset | 6.0 GB | ≈ 11.9 GB |
| 7 | mitoem2 | MitoEM 2.0, full | 17.5 GB | 17.5 GB |
| 8 | deepcontact | full | 3.2 GB | 3.2 GB |
| 9 | zenodo-15602048 | TNBC TEM, full | 2.3 GB | 2.3 GB |
| 10 | zenodo-17068504 | cardiomyocyte TEM, full | 0.5 GB | 0.5 GB |
| 11 | platynereis-zenodo | labeled training data, full | 6.8 GB | 6.8 GB |
| 12 | platelet-guay | full | 0.2 GB | 0.2 GB |
| 13 | s-biad2822 | NucleoNet, full | 2.0 GB | ≈ 3.9 GB |
| 14 | deeppi-em | Google Drive folder | 0.5 GB | 0.5 GB |
| 15 | orgsegnet | **manual**: writes `MANUAL_DOWNLOAD.txt` | (32.4 GB by hand) | |
| 16 | openorganelle-crops | 49 windows of 1000³ covering all 64 GT crops | ≈ 32 GB | ≈ 70 GB |
| 17 | empiar-13420-crops | 3 windows + labels | ≈ 1.8 GB | ≈ 40 GB |
| 18 | empiar-10791-crops | 3 windows + 7 label classes (slowest per GB) | ≈ 2.5 GB | ≈ 80 GB |
| 19 | asem-crops | 21 tiles over all labeled regions | ≈ 14 GB | ≈ 18 GB |
| 20 | fastem-crops | 3 windows (54 sections) + mito GT | ≈ 0.3 GB | ≈ 2 GB |
| | **total** (excl. OrgSegNet) | | **≈ 125 GB** | **≈ 330 GB** |

Crop sizes are estimates from compression ratios measured on 2026-10-08 (`../DATASET_SIZES.md` has the older
2000³ plan in detail). Transfer is larger
than storage where sources have no checksum and are read twice, or where whole slices or full-width rows must
be fetched to cut out a crop. The 10791 liver crops are the slowest job: their raw slices are uncompressed and
read twice, and every label slice is pulled out of a remote zip and decompressed.

## Output layout

```
$OUT/<id>/...                                  downloaded files (relative paths as on the server)
$OUT/<id>/<crop>.ome.zarr/{raw,labels/...}/s0   crops
$OUT/<id>/<crop>.json                          sidecar (also stored in the zarr attrs)
$OUT/<id>/_status.json                         per-dataset result, lists failures
$OUT/fetch_run_<id>.log                        log
```

Each array in a crop has OME-NGFF `multiscales` metadata. Its scale is the voxel size in nm, and its
translation is the crop position in the dataset's world grid, so raw and labels overlay correctly in napari,
neuroglancer or Fiji/MoBIE. `source` (and, for zarr sources, `source_group_attrs`, e.g. COSEM class ids) are
kept in the attrs.

Read a crop:
```python
import zarr
g = zarr.open_group("asem-crops/cell_46_t01.ome.zarr", mode="r")
raw, mito = g["raw/s0"][:], g["labels/mito/s0"][:]
```

## Robustness features
* HTTP retries with backoff (429/5xx, dropped connections). File downloads resume from `.part` files, and a
  file is only renamed into place after its size and md5 check out. Bad-md5 files are kept as `.part.badmd5`.
* Crops are written in z-slabs, with progress in `<crop>.ome.zarr/.progress.json`, so a killed job resumes.
* **EBI listing bug:** the EMPIAR HTTPS (and FTP) directory listings silently omit about 0.1–0.3 % of files
  (seen on 2026-10-08 in EMPIAR-13420). `http_dir` therefore takes the union of the HTTPS and FTP listings and
  HEAD-probes every gap in numbered file sequences. Crop slices are addressed by number, not by listing. A slice
  that truly does not exist is written as zeros and listed in the sidecar under `missing_source_slices`.
* **Integrity without checksums.** During testing on 2026-10-08, a few heavily parallel EBI responses had the
  correct length but different bytes. So every payload is verified somehow:
  * published md5 (Zenodo, figshare, 4TU)
  * zip CRCs (whole zips and members of the 10791 label zips)
  * gzip/zlib checksums (ASEM chunks, Platynereis N5)
  * LZW decode errors (re-fetched)
  * everything else (EMPIAR/BioStudies files without md5, uncompressed TIFF range reads, WebKnossos chunks,
    COSEM zstd chunks) is read twice and must match, with a third read deciding.

  This roughly doubles transfer for those sources, which is the price of "100 % correct". Disagreements are
  logged as `integrity:` warnings.
* A failure in one file or crop is logged and the rest continues. The task exits non-zero if anything
  failed, so `sacct` shows it. Re-submitting retries only what is missing.

## Known external issues (2026-10-08)
* **Science Data Bank (orgsegnet):** needs a browser login, so download it by hand (see `MANUAL_DOWNLOAD.txt`).
* **Google Drive (deeppi-em):** gdown can hit Drive's per-file quota. If it does, retry later.

## How crop positions were chosen
Crop edge is 1000 voxels (user decision 2026-10-12). Three placement rules, all in `dev/build_manifest.py`:
* **OpenOrganelle (label boxes):** every ground-truth crop of jrc_hela-2 (26), jrc_macrophage-2 (18) and
  jrc_jurkat-1 (20) is downloaded with its raw. A deterministic greedy cover picks 1000³ windows (19 + 16 + 14 =
  49) so that each GT crop lies completely inside one window (`gt_crops` in the manifest lists which). Windows
  may overlap. The GT `all` arrays are copied at s1 (same grid as raw) and s0 (native 2× finer).
* **ASEM (sparse painted labels):** each cell volume is split into equal tiles of ≤ 1000 voxels per axis (an
  axis whose labels all fit in one 1000 span gets a single span), and every tile that holds labeled chunks is
  kept: 8 tiles of cell 46, 7 of cell 58, 4 of cell 61, plus one 1000³ window centred on each of cell 61's small
  ER and Golgi blocks. Only the label classes present in a tile are written. ASEM annotation is sparse: 0 means
  "not annotated", not necessarily background.
* **Whole-volume labels (EMPIAR-10791, EMPIAR-13420, FAST-EM):** one 1000³ window per volume. 10791: centre of
  the former 2000³ window (labels cover the whole volume; all 7 classes). 13420: the window holding the most
  labeled voxels, measured on every ~70th label slice (2026-10-12). FAST-EM: centre of each former 2000×2000
  window inside the Mito_GT region (raw + SOFIMA-realigned raw, both with GT).
