# Labeled EM organelle datasets

Catalog and download tooling for about 20 public, labeled electron-microscopy organelle datasets.

**Current focus: labeled datasets only.** The QuantEM unlabeled corpus (`quantem/`) is analysis only and on hold.

- `metadata.csv`: one row per dataset (links, resolution, size, imaging, biosample, labels, download plan)
- `DATASET_SIZES.md` / `dataset_sizes.csv`: size at source vs. what the plan stores (≈157 GB)
- `scripts/`: downloader (`fetch_em.py`), manifest (`datasets.json`), SLURM array (`download_array.sbatch`), env setup. **Start with `scripts/README.md`.**
- `sources.csv`: original source list
- `HANDOFF.md`: status, decisions, gotchas
- `research/`: raw notes from the cataloging work
