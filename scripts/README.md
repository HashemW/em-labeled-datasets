# EM organelle datasets: download / crop scripts

Downloads the 20 public datasets listed in `../sources.csv` (catalogued in `../metadata.csv`) onto the cluster.

* Datasets **≤ 30 GB** are downloaded in full, with md5 verification where the repository publishes one.
* Datasets **> 30 GB** get **3 crops of 2000×2000×2000 voxels**. A crop is clipped where the volume is thinner,
  e.g. FAST-EM is only 54 sections. Crops are written as **OME-Zarr** (v0.4, zarr format 2, Blosc-zstd,
  chunks 64×256×256), with labels in the same file, plus a **JSON sidecar** per crop (source URLs, offsets,
  voxel size, dtype, missing slices).

## Files

| file | purpose |
|---|---|
| `fetch_em.py` | the downloader (one file, no project imports) |
| `datasets.json` | manifest: one entry per job, i.e. per dataset or per dataset part |
| `download_array.sbatch` | SLURM job array, one task per manifest entry |
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
Copy the whole folder: `fetch_em.py`, `datasets.json`, `download_array.sbatch`, `setup_env.sh`,
`requirements.txt` (and `dev/`, which is optional). Then log in and `cd ~/em_fetch`.

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
./venv/bin/python fetch_em.py --list          # should print 23 numbered datasets (0-22)
```
Then run the **smoke test**. It touches every download route with tiny reads (64³ crops, the first MiB of big
files, folder listings), writes well under 1 GB, and takes 15–30 minutes:
```bash
./venv/bin/python fetch_em.py --out /scratch/$USER/em_test --test
```
It ends with a summary table. Every line should be `OK`, with two expected exceptions:
`orgsegnet OK (MANUAL)`, and `fastem-4tu FAILED` while 4TU is down for maintenance. Use a **different**
`--out` for the test than for the real run; the script refuses to let a test overwrite real crops.
Delete the test folder afterwards.

If the login node kills long processes, run the smoke test as a job instead:
`sbatch --wrap "./venv/bin/python fetch_em.py --out /scratch/$USER/em_test --test" --time=1:00:00 --mem=8G`

### Step 5: configure the job script
Edit the header of `download_array.sbatch`:
* `--partition` / `--account`: uncomment and set them if your cluster requires them. If there is a
  data-transfer partition, use it.
* `--time`: 2 days by default. Task 19 (EMPIAR-10791 liver crops) is the longest; give it more if your
  cluster's network is slow.
* `--array=0-22%4`: `%4` = at most 4 downloads at once. That is polite to the servers; raise it if you like.
* `--mem=32G`, `--cpus-per-task=8`: enough for all tasks.

### Step 6: choose the output location and submit
Choose a filesystem with enough **quota** for everything you will keep. The default plan stores **≈ 157 GB**
(+32 GB if you add OrgSegNet by hand); see `../DATASET_SIZES.md`, and leave ~20–30 % headroom. Project or scratch space is usually right; home usually is not. Check free space
with `df -h /path` or your site's quota command.
```bash
cd ~/em_fetch
mkdir -p logs
OUT=/project/mylab/em_datasets sbatch download_array.sbatch
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
OUT=/project/mylab/em_datasets sbatch --array=12,19 download_array.sbatch
```
To run a single dataset without SLURM (e.g. on the transfer node):
`./venv/bin/python fetch_em.py --out $OUT --only empiar-12885`

After a successful run, the one manual dataset (OrgSegNet) is described in `$OUT/orgsegnet/MANUAL_DOWNLOAD.txt`.

### Troubleshooting
| symptom | cause / fix |
|---|---|
| `fastem-4tu` fails with `too many 503 error responses` | 4TU storage maintenance. Re-submit task 12 later. |
| `deeppi-em` fails mentioning quota/permission | Google Drive rate limit. Re-submit task 15 after a few hours. |
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
| 11 | platynereis-zenodo | training data, full | 6.8 GB | 6.8 GB |
| 12 | fastem-4tu | FAST-EM sample zip, full | 7.3 GB | 7.3 GB |
| 13 | platelet-guay | full | 0.2 GB | 0.2 GB |
| 14 | s-biad2822 | NucleoNet, full | 2.0 GB | ≈ 3.9 GB |
| 15 | deeppi-em | Google Drive folder | 0.5 GB | 0.5 GB |
| 16 | orgsegnet | **manual**: writes `MANUAL_DOWNLOAD.txt` | (32.4 GB by hand) | |
| 17 | openorganelle-crops | 3 crops + GT | ≈ 14.5 GB | ≈ 35 GB |
| 18 | empiar-13420-crops | 3 crops + labels | ≈ 7.5 GB | ≈ 26 GB |
| 19 | empiar-10791-crops | 3 crops + 7 label classes (slowest) | ≈ 19.7 GB | ≈ 267 GB |
| 20 | asem-crops | 3 crops + labels | ≈ 9.7 GB | ≈ 12 GB |
| 21 | fastem-crops | 3 crops + mito GT | ≈ 1.3 GB | ≈ 13 GB |
| 22 | platynereis-crops | 3 raw crops | ≈ 21.7 GB | ≈ 26 GB |
| | **total** (excl. OrgSegNet) | | **≈ 157 GB** | **≈ 500 GB** |

Full numbers, how each was measured, and per-crop detail are in `../DATASET_SIZES.md`. Transfer is larger
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
g = zarr.open_group("asem-crops/cell_46_crop.ome.zarr", mode="r")
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
* **4TU (fastem-4tu):** storage was "temporarily unavailable for maintenance" (HTTP 503) when tested. The task
  retries, then fails cleanly; re-submit task 12 later.
* **Science Data Bank (orgsegnet):** needs a browser login, so download it by hand (see `MANUAL_DOWNLOAD.txt`).
* **Google Drive (deeppi-em):** gdown can hit Drive's per-file quota. If it does, retry later.

## How crop positions were chosen
* **OpenOrganelle:** in each of jrc_hela-2, jrc_macrophage-2 and jrc_jurkat-1, the 2000³ window that fully
  contains the most ground-truth crops (6, 4 and 6). The GT `all` arrays are copied at s1 (same grid as raw)
  and s0 (native 2× finer).
* **EMPIAR-13420:** the two FIB-SEM cells and the largest SBF-SEM cell, with each window over the labelled
  nucleus/organelles (label extents measured from sampled label slices).
* **EMPIAR-10791:** one crop per specimen (lean, obese1, obese2), centred in the volume. Includes all 7
  segmentation classes.
* **ASEM:** cells 46, 58 (mito/ER/Golgi labels) and 61 (mito, plus small ER/Golgi blocks). The window is placed
  over the non-empty label chunks.
* **FAST-EM:** the three non-overlapping 2000×2000 windows with the highest mito-GT coverage.
* **Platynereis:** three non-overlapping windows whose low-resolution (s6) tissue mask is 100 % animal tissue.
