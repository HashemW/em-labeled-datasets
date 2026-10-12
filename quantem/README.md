# QuantEM dataset directory: sizes vs. 2000×2000×2000

Source: <https://arrojodrigolab.github.io/QuantEM/dataset_directory/>. Its published data files
(`data/datasets.json`, `data/assets.json`, `data/facets.json`) are a snapshot dated **2026-08-07**: 657 datasets
made of 17,828 items (16,107 2D images and 1,721 3D volumes). The site lists width, height, slice count and pixel
size for every item, so this table was built from those numbers without visiting each repository.

Full table: `quantem_dataset_sizes.csv` (one row per dataset, sorted by category, then size).

## Answer

| category | meaning | datasets |
|---|---|---:|
| **A** | at least one volume is ≥ 2000 px in x, y **and** z, so a full 2000³ crop fits | **87** |
| **B** | large overall (≥ 8 billion voxels = 2000³ worth) but no single volume reaches 2000 in every dimension, usually because it has < 2000 sections | **140** |
| **C** | 3D, smaller than 2000³ | **185** |
| **D** | 3D, but the site does not list the slice count for some volumes, so it can't be decided from the site | **33** |
| **E** | 2D images only (no volume) | **212** |
| | total | 657 |

So **87 datasets definitely contain a ≥ 2000³ volume**, 140 more are large but too thin in one direction for a
full cube, and 33 need a look at the repository to decide.

Under the crop rule used for our own datasets (a crop is clipped where the volume is thinner), the 140
category-B datasets would still give large crops, e.g. 2000×2000×1024.

## Columns in the CSV
| column | meaning |
|---|---|
| `category` | A–E, as above |
| `dataset`, `experiment`, `repository`, `url` | as listed on the site |
| `n_volumes_3d`, `n_images_2d` | number of 3D volumes and 2D images in the dataset |
| `n_volumes_ge_2000cubed` | how many volumes are ≥ 2000 in all three dimensions |
| `n_volumes_unknown_z` | volumes whose slice count the site doesn't list |
| `largest_volume_xyz` | the largest volume by voxel count (x×y×z, pixels/slices) |
| `largest_cube_edge_that_fits` | the biggest cube edge that fits inside any one volume (≥ 2000 means category A) |
| `total_3d_voxels_G`, `total_2d_pixels_G` | sum over the dataset, in billions |
| `approx_uncompressed_GB_8bit` | the data size if stored as 8-bit, uncompressed (1 voxel = 1 byte) |
| `pixel_size_nm`, `modality`, `species`, `organ` | as listed on the site ("not listed" if missing) |

## Caveats
* **Dimensions come from the site, not from each repository.** I cross-checked the 8 datasets I had measured
  directly (EMPIAR-10791, -13420, -11746, -13156, -10994, S-BIAD2822, ASEM, OpenOrganelle jrc_hela-2), and every
  verdict agreed. The site does round some sizes: it lists jrc_hela-2 as 12000×1500×6298, while the
  real array is 12000×1600×6368. Datasets very close to 2000 in some dimension may therefore deserve a direct
  check.
* **"Size" here means voxels (data volume), not file size on the server.** The site does not publish file
  sizes, and real storage differs from the 8-bit figure. Many sources are 16-bit (2×), most are compressed,
  and some huge connectomics volumes (MICrONS, FlyWire, the hemibrain) are multi-petavoxel. Getting actual
  stored bytes would mean querying each of the 14 repositories separately: quick for EMPIAR/Zenodo/figshare,
  slow for the BossDB/OpenOrganelle/WebKnossos buckets. That is well over an hour for all 657, so it was not
  done.
* **Category D (33 datasets)** would need each repository opened by hand: about 2–5 minutes each, so roughly
  1–2.5 hours. Not done; they are listed in the CSV with their repository links.
* The directory notes that the corpus grows. This reflects the 2026-08-07 snapshot it served on 2026-10-08.
