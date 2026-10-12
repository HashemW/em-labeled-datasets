# Dataset sizes: full totals vs. what the download plan stores

Measured on 2026-10-08. All sizes are decimal (1 GB = 10^9 bytes, 1 TB = 10^12 bytes). The machine-readable version, with per-array crop detail, is `dataset_sizes.csv`.

## Bottom line

| | size |
|---|---|
| **Everything at the source, in full** (20 datasets, exactly as hosted) | **10.35 TB** |
| **Stored on disk by the current plan** (full downloads + 3 crops for big sets), excl. manual OrgSegNet | **156.5 GB** |
| + OrgSegNet if downloaded by hand | +32.4 GB |
| **Network transfer for the current plan** (incl. integrity double-reads), excl. OrgSegNet | **≈ 497.6 GB** |

Almost all of the full-size total sits in the datasets that get cropped: OpenOrganelle / COSEM (3.04 TB), EMPIAR-10791 (2.80 TB), Platynereis (2.16 TB), ASEM / incasem (1.72 TB), FAST-EM (460.7 GB), EMPIAR-13420 (37.9 GB). Together they hold 10.22 TB at the source; under the plan they take 88.4 GB on disk.

**Disk headroom.** "Stored" is what ends up in `$OUT`. The script needs no extra temporary space: files download to a `.part` file that is renamed in place, crops are written chunk by chunk, and zips are not unpacked. If you later unpack the zips (MitoEM 2.0, DeepContact, CEM-MitoLab, Zenodo 10043461, platelet), allow roughly another 1-2x their size. Keeping ~20-30 % free on top is sensible.

## Per dataset

| dataset | total at source | how the total was obtained | plan | stored by plan | transfer by plan |
|---|---:|---|---|---:|---:|
| CEM-MitoLab (EMPIAR-11037) | 2.8 GB | EMPIAR API `dataset_size` | full | 2.8 GB | ≈ 2.8 GB |
| MitoEM 2.0 (Zenodo 17635006) | 17.5 GB | Zenodo API (sum of 8 files, exact) | full | 17.5 GB | ≈ 17.5 GB |
| OrgSegNet (ScienceDB 01335) | 32.4 GB | DataCite metadata (exact; 6,076 files) | manual (browser) | 32.4 GB | ≈ 32.4 GB |
| DeepContact (figshare 19898404) | 3.2 GB | figshare API (exact) | full | 3.2 GB | ≈ 3.2 GB |
| TNBC TEM (Zenodo 15602048) | 2.3 GB | Zenodo API (exact) | full | 2.3 GB | ≈ 2.3 GB |
| Cardiomyocyte TEM (Zenodo 17068504) | 0.5 GB | Zenodo API (exact) | full | 0.5 GB | ≈ 0.5 GB |
| OpenOrganelle / COSEM (3 volumes used) | 3.04 TB | S3 bucket listing of jrc_hela-2/, jrc_macrophage-2/, jrc_jurkat-1/ (exact; all formats, labels, predictions) | 3 crops | 14.5 GB | ≈ 34.8 GB |
| EMPIAR-13420 (macrophage / A431) | 37.9 GB | EMPIAR API `dataset_size` | 3 crops | 7.5 GB | ≈ 25.9 GB |
| EMPIAR-10994 (HeLa SBF-SEM) | 8.7 GB | EMPIAR API `dataset_size` | full | 8.7 GB | ≈ 17.4 GB |
| EMPIAR-13156 (HeLa STARD3) | 10.3 GB | EMPIAR API `dataset_size` | full | 10.3 GB | ≈ 20.6 GB |
| FAST-EM (TU Delft, MCF-7) | 460.7 GB | computed: WebKnossos EM layer 93184x90112x54 uint8 (served uncompressed; no listing) + 4TU API (exact, 7.3 GB) | 3 crops + 4TU zip | 8.6 GB | ≈ 20.6 GB |
| DeepPI-EM (Google Drive) | 0.5 GB | measured: full download on 2026-10-08 (Drive shows no folder total) | full | 0.5 GB | ≈ 0.5 GB |
| EMPIAR-10982 (MitoNet benchmarks) | 4.1 GB | EMPIAR API `dataset_size` | full | 4.1 GB | ≈ 8.2 GB |
| EMPIAR-12885 (AIVE) | 32.8 GB | EMPIAR API `dataset_size`; subset by HEAD of the 24 selected files (exact) | useful subset | 6.0 GB | ≈ 11.9 GB |
| ASEM / incasem | 1.72 TB | S3 bucket listing of s3://asem-project (exact, 5.98 M objects) | 3 crops | 9.7 GB | ≈ 12.3 GB |
| Guay et al. platelet | 0.2 GB | measured: full download (Dropbox sends no size header) | full | 0.2 GB | ≈ 0.2 GB |
| EMPIAR-11746 + Zenodo 10043461 (U2-OS) | 10.1 GB | EMPIAR API + Zenodo API | full (both) | 10.1 GB | ≈ 15.3 GB |
| EMPIAR-10791 (mouse liver) | 2.80 TB | EMPIAR API `dataset_size` (README: 17 TB once segmentation zips are unpacked) | 3 crops | 19.7 GB | ≈ 267.1 GB |
| Platynereis (Zenodo 3675220 + raw N5) | 2.16 TB | Zenodo API (6.8 GB, exact) + bucket listing of the raw N5 pyramid (exact; s0 = 1.68 TB stored, 8.1 TB bounding box uncompressed) | Zenodo full + 3 crops | 28.5 GB | ≈ 32.4 GB |
| S-BIAD2822 (NucleoNet) | 2.0 GB | BioStudies file lists (sum, exact) | full | 2.0 GB | ≈ 3.9 GB |

*Notes:* **CEM-MitoLab (EMPIAR-11037)**: one zip: CRC-checked, read once. **MitoEM 2.0 (Zenodo 17635006)**: md5-verified. **OrgSegNet (ScienceDB 01335)**: 2D images: crops not applicable. **DeepContact (figshare 19898404)**: md5-verified. **TNBC TEM (Zenodo 15602048)**: md5-verified. **Cardiomyocyte TEM (Zenodo 17068504)**: md5-verified. **OpenOrganelle / COSEM (3 volumes used)**: 8-bit EM zarr actually read from: jrc_hela-2 73 GB, jrc_macrophage-2 94 GB, jrc_jurkat-1 122 GB. Bucket holds ~100 more volumes (not counted). **EMPIAR-10994 (HeLa SBF-SEM)**: double-read verified (no md5 published). **EMPIAR-13156 (HeLa STARD3)**: double-read verified. **FAST-EM (TU Delft, MCF-7)**: 4TU part md5-verified; 4TU down for maintenance on 2026-10-08. **EMPIAR-10982 (MitoNet benchmarks)**: double-read verified. **EMPIAR-12885 (AIVE)**: skips predictions/CLAHE/duplicates (user decision). **ASEM / incasem**: labelled cells alone: 46 = 68 GB, 58 = 70 GB, 61 = 208 GB (mostly predictions). **Guay et al. platelet**: zip CRC-checked. **EMPIAR-10791 (mouse liver)**: raw rows read twice (uncompressed source). **S-BIAD2822 (NucleoNet)**: double-read verified.

## Can the totals be read off the sites?

Mostly yes. Exceptions are marked in the table:

* **Shown directly by the repository:** EMPIAR (`dataset_size` in its API and on each entry page, rounded to 0.1 GB), Zenodo, figshare and 4TU (exact bytes per file in their APIs), BioStudies (file lists with byte sizes), Science Data Bank (total in its DataCite record).
* **Not shown anywhere; I measured them:** OpenOrganelle, ASEM and the Platynereis N5 are S3 buckets with no published total, so I summed the sizes of every object (6–11 million objects per bucket prefix). The FAST-EM WebKnossos volume has no file listing, so its size is computed from its shape. Google Drive and Dropbox report no folder/file total, so I used the size of a real download.

## The 2000×2000×2000 crops

**Rule of thumb.** One 2000³ crop of 8-bit EM is **8.0 GB uncompressed**, so 3 crops = 24 GB. EM images are noisy and barely compress: measured on-disk ratios for raw EM were 0.59–0.98 (Blosc-zstd), so budget **~5–8 GB per raw crop on disk**. Labels compress extremely well. Binary masks were 0.03–2.6 % of their uncompressed size, and instance/multi-class labels were ≤ 6 %, so labels add little disk space even when they are uint32 (32 GB uncompressed per 2000³). In RAM, during processing, they are full size.

Thin volumes are clipped (e.g. FAST-EM is only 54 sections, so a crop is 2000×2000×54 = 0.22 GB).

| dataset | crop | raw shape (z×y×x) | arrays | uncompressed | est. on disk | est. transfer |
|---|---|---|---:|---:|---:|---:|
| asem-crops | cell_46_crop | 1921×1100×2000 | 4 | 54.9 GB | 3.3 GB | ≈ 4.3 GB |
| asem-crops | cell_58_crop | 1982×1000×2000 | 4 | 51.5 GB | 2.6 GB | ≈ 3.4 GB |
| asem-crops | cell_61_crop | 1805×1400×2000 | 4 | 25.4 GB | 3.7 GB | ≈ 4.6 GB |
| empiar-10791-crops | 6461_lean_crop | 2000×2000×2000 | 8 | 88.0 GB | 6.8 GB | ≈ 110.1 GB |
| empiar-10791-crops | 6464_obese1_crop | 2000×2000×2000 | 8 | 88.0 GB | 6.4 GB | ≈ 84.0 GB |
| empiar-10791-crops | 9430_obese2_crop | 2000×2000×2000 | 8 | 64.0 GB | 6.5 GB | ≈ 73.0 GB |
| empiar-13420-crops | FIB_240729_HumanMP_TNFa_1U_crop | 2000×785×2000 | 2 | 6.3 GB | 2.9 GB | ≈ 8.1 GB |
| empiar-13420-crops | FIB_240801_HumanMP_Control_crop | 2000×1054×2000 | 2 | 8.4 GB | 3.7 GB | ≈ 8.4 GB |
| empiar-13420-crops | SBF_240424_Microphages_no_load_crop | 257×2000×2000 | 2 | 2.1 GB | 0.9 GB | ≈ 9.4 GB |
| fastem-crops | MCF7_UAc_crop1 | 54×2000×2000 | 4 | 2.2 GB | 0.4 GB | ≈ 4.4 GB |
| fastem-crops | MCF7_UAc_crop2 | 54×2000×2000 | 4 | 2.2 GB | 0.4 GB | ≈ 4.4 GB |
| fastem-crops | MCF7_UAc_crop3 | 54×2000×2000 | 4 | 2.2 GB | 0.4 GB | ≈ 4.4 GB |
| openorganelle-crops | jrc_hela-2_crop | 2000×1600×2000 | 13 | 7.4 GB | 4.9 GB | ≈ 11.7 GB |
| openorganelle-crops | jrc_jurkat-1_crop | 2000×2000×2000 | 13 | 8.4 GB | 4.7 GB | ≈ 11.2 GB |
| openorganelle-crops | jrc_macrophage-2_crop | 2000×2000×2000 | 9 | 8.8 GB | 4.9 GB | ≈ 11.8 GB |
| platynereis-crops | sbem-6dpf-1_crop1 | 2000×2000×2000 | 1 | 8.0 GB | 7.2 GB | ≈ 8.6 GB |
| platynereis-crops | sbem-6dpf-1_crop2 | 2000×2000×2000 | 1 | 8.0 GB | 7.1 GB | ≈ 8.4 GB |
| platynereis-crops | sbem-6dpf-1_crop3 | 2000×2000×2000 | 1 | 8.0 GB | 7.3 GB | ≈ 8.7 GB |
| **openorganelle-crops total** | 3 crops | | | **24.6 GB** | **14.5 GB** | **≈ 34.8 GB** |
| **empiar-13420-crops total** | 3 crops | | | **16.8 GB** | **7.5 GB** | **≈ 25.9 GB** |
| **empiar-10791-crops total** | 3 crops | | | **240.0 GB** | **19.7 GB** | **≈ 267.1 GB** |
| **asem-crops total** | 3 crops | | | **131.9 GB** | **9.7 GB** | **≈ 12.3 GB** |
| **fastem-crops total** | 3 crops | | | **6.5 GB** | **1.3 GB** | **≈ 13.3 GB** |
| **platynereis-crops total** | 3 crops | | | **24.0 GB** | **21.7 GB** | **≈ 25.6 GB** |

Why transfer exceeds disk for some datasets:
* **EMPIAR-10791:** the raw TIFF slices are uncompressed and are read as full-width 2000-row bands, twice each for integrity.
* **EMPIAR-13420:** whole LZW slices are downloaded.
* **FAST-EM:** WebKnossos serves uncompressed chunks, which are read twice.
* **OpenOrganelle:** zstd chunks are read twice.
* **ASEM and Platynereis:** sources are gzip/zlib-checksummed, so they are read once.

## How the crop numbers were obtained

* **Shapes:** exact, from each source's own metadata, clipped to the volume. These are the shapes the script writes.
* **Raw on-disk:** each raw array's measured compression ratio (a real 256³, or 64-slice, sample crop written by `fetch_em.py` with the production codec) times its uncompressed size. A complete real FAST-EM crop written on the laptop matched: 0.21 GB per raw array.
* **Labels on-disk:** a full-width slab through the middle of each actual crop window (16 slices for slice stacks, 64 otherwise), compressed with the same codec.
* **Transfer:** a model of what the script reads. It accounts for compressed source chunks with chunk-alignment overhead, whole LZW slices, uncompressed row bands, and the 2× integrity re-reads where the source has no checksum. Treat it as ±25 %.
