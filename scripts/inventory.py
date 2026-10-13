#!/usr/bin/env python3
"""Inventory of exactly what the download plan takes: one CSV row per data item, with its full statistics.

An item is one 3D array (a crop array, a NIfTI/TIFF/Amira/HDF5/N5 volume, or a numbered TIFF slice series),
a set of 2D images in one folder, or a folder of Labelme polygon annotations.

Usage
-----
  # after the download (authoritative; reads every file, computes label + intensity statistics):
  python inventory.py --out /path/to/em_datasets --csv /path/to/em_datasets/items.csv
  # before the download (no download; reads file headers over HTTP, samples big 2D image sets):
  python inventory.py --remote --csv planned_items.csv

Columns: see FIELDS. Voxel sizes come, in order of preference, from the file header, from the repository
metadata stored with the array (crops), or from the paper / repository page (VOXEL_TABLE / metadata.csv);
`voxel_source` says which. `voxel_in_header` keeps the raw header value so it can be checked.
"""
import argparse
import collections
import contextlib
import csv
import gzip
import io
import json
import logging
import os
import re
import struct
import sys
import tarfile
import tempfile
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_em as F  # noqa: E402

log = logging.getLogger("inventory")

FIELDS = [
    "dataset_id", "dataset", "item", "role", "structure", "path", "source_url", "region_offset_zyx_vox",
    "n_files", "n_images", "shape_z", "shape_y", "shape_x", "shape_yx_median", "dtype", "bits",
    "voxel_z_nm", "voxel_y_nm", "voxel_x_nm", "voxel_source", "voxel_in_header",
    "physical_z_um", "physical_y_um", "physical_x_um", "n_voxels", "bytes_uncompressed", "bytes_on_disk",
    "format", "label_classes", "label_values_n", "label_values", "n_objects", "labeled_fraction",
    "intensity_min", "intensity_max", "intensity_mean", "intensity_std",
    "imaging", "organism", "cell_type", "stats_basis", "notes"]

IMG_EXT = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".nii", ".nii.gz", ".am", ".dm4", ".h5", ".hdf5", ".mrc")
LABEL_RX = re.compile(r"(label|mask|lbl_|groundtruth|/gt[_/]|segmentation|model_tif|_mito\.|mitoclass|organellemodel|"
                      r"celloutlinemodel|_myo\.|_cell\.|_nucleus\.|_nucleolus\.|- human|- aive|_seg\b|"
                      r"/(er|endosomes|mitochondria)(_roi)?\.tiff?$)", re.I)  # last: EMPIAR-13156 segmentations
PRED_RX = re.compile(r"(prediction|predicted|probabilit)", re.I)
RAW_DIR_RX = re.compile(r"/(images|imagesTr|imagesTs|raw)(/|$)", re.I)
SKIP_RX = re.compile(r"(^|/)(__MACOSX|\.DS_Store|_status(_test)?\.json$|fetch_(run|test)_.*\.log$|MANUAL_DOWNLOAD\.txt$)")

# Voxel sizes (nm, z/y/x) from the papers / repository pages, for files whose header has none (or a wrong one).
# (dataset id, regex on the item path) -> (z, y, x, source). z None = 2D.
VOXEL_TABLE = [
    ("empiar-10994", r"160621_HeLa_Control", (30, 15.2, 15.2, "EMPIAR-10994 entry")),
    ("empiar-10994", r"161107_HeLa_REEP3-4", (30, 14.8, 14.8, "EMPIAR-10994 entry")),
    ("empiar-11746", r".", (5, 2.5, 2.5, "EMPIAR-11746 entry")),
    ("empiar-13156", r"HeLaSTARD3_WT_Raw", (None, 5, 5, "EMPIAR-13156 entry (pixel 50 A; z not stated)")),
    ("empiar-13156", r"HeLaSTARD3_FA_YA_Raw|HeLaWT_Raw", (None, 8, 8, "EMPIAR-13156 entry (pixel 80 A; z not stated)")),
    ("zenodo-10043461", r".", (5, 2.5, 2.5, "EMPIAR-11746 entry (same cell)")),
    ("empiar-12885", r"DATASET1_TEST_ROI", (10, 3.26, 3.26, "EMPIAR-12885 entry")),
    ("empiar-12885", r"Set4", (20, 16, 16, "EMPIAR-12885 entry (muscle)")),
    ("empiar-12885", r"Set[1-3]", (10, 6.51, 6.51, "EMPIAR-12885 entry (half-scale overviews)")),
    ("empiar-10982", r"c_elegans", (None, 24, 24, "MitoNet paper, Table S1 (z not stated)")),
    ("empiar-10982", r"fly_brain", (None, 12, 12, "MitoNet paper, Table S1 (z not stated)")),
    ("empiar-10982", r"glycolytic_muscle", (None, 18, 18, "MitoNet paper, Table S1 (z not stated)")),
    ("empiar-10982", r"hela_cell", (None, 15, 15, "MitoNet paper, Table S1 (z not stated)")),
    ("empiar-10982", r"lucchi_pp", (5, 5, 5, "Lucchi++ (isotropic 5 nm)")),
    ("empiar-10982", r"salivary_gland", (None, 15, 15, "EMPIAR-10982 entry (z not stated)")),
    ("platelet-guay", r".", (50, 10, 10, "Guay et al. 2021")),
    ("platynereis-zenodo", r"(^|/)(membrane|cilia)", (25, 20, 20, "Zenodo 3675220 record description")),
    ("platynereis-zenodo", r"(^|/)cuticle", (50, 40, 40, "Zenodo 3675220 record description")),
    ("platynereis-zenodo", r"(^|/)nuclei", (100, 80, 80, "Zenodo 3675220 record description")),
    ("deepcontact", r"sem/|sem_data", (None, 10, 10, "DeepContact paper (Sertoli SEM)")),
    ("deepcontact", r"ROTO_SEM_5nm|cell_data", (None, 5, 5, "DeepContact paper (U-2 OS SEM)")),
    ("deepcontact", r"tem/|tem_data", (None, 4.68, 4.68, "DeepContact paper (COS-7 TEM)")),
]


# ----------------------------------------------------------------------------- metadata.csv join

def load_meta(path):
    meta = {}
    if not os.path.exists(path):
        return meta
    for r in csv.DictReader(open(path, encoding="utf-8")):
        for mid in re.split(r"\s*;\s*", r.get("manifest_ids", "")):
            if mid:
                meta[mid] = r
    return meta


def table_voxel(did, path):
    for d, rx, v in VOXEL_TABLE:
        if d == did and re.search(rx, path):
            return v
    return None


# ----------------------------------------------------------------------------- generic file access

class Entry:
    """A file to inspect: local path, remote URL, or a member of a zip/tar (opened lazily)."""

    def __init__(self, did, path, size, opener, url=""):
        self.did, self.path, self.size, self.opener, self.url = did, path, size, opener, url

    @property
    def ext(self):
        p = self.path.lower()
        return ".nii.gz" if p.endswith(".nii.gz") else os.path.splitext(p)[1]


def _zip_entries(did, zpath, url, fileobj_factory, size):
    out = []
    try:
        zf = zipfile.ZipFile(fileobj_factory())
    except Exception as e:
        log.warning("cannot open zip %s: %r", zpath, e)
        return [Entry(did, zpath, size, None, url)]
    for info in zf.infolist():
        if info.is_dir() or SKIP_RX.search("/" + info.filename):
            continue
        out.append(Entry(did, f"{zpath}!/{info.filename}", info.file_size,
                         (lambda zf=zf, n=info.filename: zf.open(n)), url))
    return out


def _tar_entries(did, tpath, local_path):
    """Local tar(.gz): members are read in archive order (streaming)."""
    out = []
    with tarfile.open(local_path, "r:*") as tf:
        names = [(m.name, m.size) for m in tf.getmembers() if m.isfile() and not SKIP_RX.search("/" + m.name)]
    for n, s in names:
        out.append(Entry(did, f"{tpath}!/{n}", s, (lambda n=n: _tar_member(local_path, n))))
    return out


def _tar_member(local_path, name):
    tf = tarfile.open(local_path, "r:*")
    return tf.extractfile(name)


def local_entries(did, root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.endswith(".ome.zarr") and d != "__MACOSX"]
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if SKIP_RX.search(rel) or fn.endswith(".part"):
                continue
            if fn.endswith(".json") and os.path.isdir(full[:-5] + ".ome.zarr"):
                continue  # crop sidecar
            size = os.path.getsize(full)
            if fn.lower().endswith(".zip"):
                out += _zip_entries(did, rel, "", lambda f=full: open(f, "rb"), size)
                out.append(Entry(did, rel, size, None))  # the archive itself (for bytes on disk)
            elif re.search(r"\.(tar|tar\.gz|tgz)$", fn.lower()):
                out += _tar_entries(did, rel, full)
                out.append(Entry(did, rel, size, None))
            else:
                out.append(Entry(did, rel, size, lambda f=full: open(f, "rb")))
    return out


def remote_entries(ds):
    did = ds["id"]
    files = []
    if ds["kind"] == "files":
        files = [(f["dest"], f["url"], f.get("size")) for f in ds["files"]]
    elif ds["kind"] == "http_dir":
        inc = [re.compile(r) for r in ds.get("include", [".*"])]
        exc = [re.compile(r) for r in ds.get("exclude", [])]
        for base in ds["urls"]:
            for rel, u in F.list_files(base, ftp_check=False, probe_gaps=False):
                if any(r.search(rel) for r in inc) and not any(r.search(rel) for r in exc):
                    files.append((rel, u, None))
    out = []
    for rel, u, size in files:
        if SKIP_RX.search(rel):
            continue
        low = rel.lower()
        if low.endswith(".zip"):
            try:
                hf = F.HTTPFile(u, size)
                size = hf.size
                out += _zip_entries(did, rel, u, lambda hf=hf: io.BufferedReader(hf, 1 << 20), size)
            except Exception as e:
                log.warning("remote zip %s not readable (%r); listed without contents", rel, e)
            out.append(Entry(did, rel, size, None, u))
        elif re.search(r"\.(tar|tar\.gz|tgz)$", low):
            out.append(Entry(did, rel, size, None, u))  # tar.gz cannot be read remotely without streaming it all
        else:
            out.append(Entry(did, rel, size, (lambda u=u, s=size: io.BufferedReader(F.HTTPFile(u, s), 1 << 16)), u))
    return out


@contextlib.contextmanager
def seekable(entry, max_mem=1 << 27):
    """Yield a seekable binary file for an entry (zip/tar members are buffered in memory or a temp file)."""
    f = entry.opener()
    if f.seekable() and not isinstance(f, zipfile.ZipExtFile):
        try:
            yield f
        finally:
            f.close()
        return
    if (entry.size or 0) <= max_mem:
        data = f.read()
        f.close()
        yield io.BytesIO(data)
        return
    with tempfile.TemporaryFile() as t:
        while True:
            b = f.read(1 << 24)
            if not b:
                break
            t.write(b)
        f.close()
        t.seek(0)
        yield t


# ----------------------------------------------------------------------------- statistics accumulator

class Stats:
    def __init__(self, label):
        self.label = label
        self.n = 0
        self.nz = 0
        self.vals = set()
        self.too_many = False
        self.mn, self.mx, self.s, self.s2 = None, None, 0.0, 0.0

    def add(self, a):
        a = np.asarray(a).reshape(-1)
        for i in range(0, a.size, 1 << 24):  # bounded memory: 16 M elements at a time
            self._add(a[i:i + (1 << 24)])

    def _add(self, a):
        if a.size == 0:
            return
        self.n += a.size
        if self.label:
            self.nz += int(np.count_nonzero(a))
            if not self.too_many:
                self.vals.update(np.unique(a).tolist())
                if len(self.vals) > 5_000_000:
                    self.too_many = True
        else:
            af = a.astype(np.float64)
            self.mn = float(af.min()) if self.mn is None else min(self.mn, float(af.min()))
            self.mx = float(af.max()) if self.mx is None else max(self.mx, float(af.max()))
            self.s += float(af.sum())
            self.s2 += float((af * af).sum())

    def row(self):
        if not self.n:
            return {}
        if self.label:
            vals = sorted(self.vals)
            nonzero = [v for v in vals if v != 0]
            return {"label_values_n": ">5000000" if self.too_many else len(vals),
                    "label_values": " ".join(str(int(v)) for v in vals[:20]) + (" ..." if len(vals) > 20 else ""),
                    "n_objects": "" if self.too_many else len(nonzero),
                    "labeled_fraction": round(self.nz / self.n, 6)}
        mean = self.s / self.n
        return {"intensity_min": self.mn, "intensity_max": self.mx, "intensity_mean": round(mean, 3),
                "intensity_std": round(max(0.0, self.s2 / self.n - mean * mean) ** 0.5, 3)}


# ----------------------------------------------------------------------------- per-format readers
# Each returns dict(shape_zyx, dtype, voxel_nm_zyx (or None), voxel_in_header, format) and, if stats is given,
# feeds the data into it plane by plane.

def _unit(u):
    """ImageJ length unit -> ascii: '\\u00B5m' (escaped, as ImageJ writes it), 'µm', 'μm' -> 'um'."""
    return str(u).strip().lower().replace("\\u00b5", "u").replace("µ", "u").replace("μ", "u")


def _tiff_voxel(tf, page):
    """Pixel size from TIFF metadata (nm, z/y/x) + the raw header text it came from."""
    hdr = []
    z = y = x = None
    ij = tf.imagej_metadata or {}
    if ij:
        hdr.append(f"imagej unit={ij.get('unit')} spacing={ij.get('spacing')}")
    fei = getattr(tf, "fei_metadata", None)
    if fei:
        pw = (fei.get("Scan") or {}).get("PixelWidth") or (fei.get("EScan") or {}).get("PixelWidth")
        if pw:
            hdr.append(f"FEI PixelWidth={pw} m")
            y = x = float(pw) * 1e9
    sem = getattr(tf, "sem_metadata", None)
    if sem and x is None:
        ps = sem.get("ap_image_pixel_size") or sem.get("ap_pixel_size")
        if ps:
            hdr.append(f"Zeiss ap_image_pixel_size={ps}")
            try:
                v, u = ps[1], ps[2] if len(ps) > 2 else ""
                y = x = float(v) * {"nm": 1, "um": 1e3, "µm": 1e3, "pm": 1e-3}.get(str(u).strip(), 1)
            except Exception:
                pass
    try:
        xr = page.tags.get("XResolution")
        ru = page.tags.get("ResolutionUnit")
        if xr is not None:
            num, den = xr.value
            ppu = num / den if den else 0
            unit = int(ru.value) if ru is not None else 2
            hdr.append(f"XResolution={ppu:g}/unit{unit}")
            if x is None and ppu > 0 and ppu != 1:  # 1 px per unit = resolution never set
                if ij and ij.get("unit"):
                    u = _unit(ij["unit"])
                    f = {"nm": 1, "nanometer": 1, "um": 1e3, "micron": 1e3, "microns": 1e3, "mm": 1e6}.get(u)
                    if f:
                        y = x = f / ppu
                elif unit == 3:
                    y = x = 1e7 / ppu
                elif unit == 2 and ppu not in (72, 96, 150, 300, 600):
                    y = x = 2.54e7 / ppu
            if ij and ij.get("spacing") and z is None:
                u = _unit(ij.get("unit", ""))
                f = {"nm": 1, "nanometer": 1, "um": 1e3, "micron": 1e3, "microns": 1e3}.get(u)
                if f:
                    z = float(ij["spacing"]) * f
    except Exception:
        pass
    if x is not None and not (0.2 < x < 500):
        hdr.append("(implausible, ignored)")
        x = y = None
    return (z, y, x) if x is not None else None, "; ".join(hdr)


class _Blocks:
    """Read-through block cache over a remote file: walking TIFF IFDs costs one request per block, not per page."""

    def __init__(self, f, block=2 << 20, budget=256 << 20):
        self.f, self.block, self.budget, self.fetched, self.cache = f, block, budget, 0, {}

    def read_at(self, off, n):
        out = b""
        while n > 0:
            hit = next(((s, d) for s, d in self.cache.items() if s <= off < s + len(d)), None)
            if hit is None:
                if self.fetched >= self.budget:
                    raise EOFError("budget")
                if len(self.cache) > 32:
                    self.cache.pop(next(iter(self.cache)))
                # small reads first (a 2D TIFF has one IFD), big blocks once it is clearly a stack
                size = 64 << 10 if len(self.cache) < 3 and self.fetched < (1 << 20) else self.block
                self.f.seek(off)
                hit = (off, self.f.read(size))
                self.cache[off] = hit[1]
                self.fetched += len(hit[1])
            s, d = hit
            d = d[off - s: off - s + n]
            if not d:
                break
            out, off, n = out + d, off + len(d), n - len(d)
        return out


def _count_ifds(fh):
    """Number of pages in a remote TIFF -> (n, exact). Past the read budget, estimate from the IFD spacing."""
    c = _Blocks(fh)
    h = c.read_at(0, 16)
    e = "<" if h[:2] == b"II" else ">"
    big = struct.unpack(e + "H", h[2:4])[0] == 43
    off = struct.unpack(e + ("Q" if big else "I"), h[8:16] if big else h[4:8])[0]
    n, last = 0, 0
    try:
        while off and n < 10 ** 6:
            n, last = n + 1, off
            k = struct.unpack(e + ("Q" if big else "H"), c.read_at(off, 8 if big else 2))[0]
            p = off + (8 + 20 * k if big else 2 + 12 * k)
            off = struct.unpack(e + ("Q" if big else "I"), c.read_at(p, 8 if big else 4))[0]
    except EOFError:
        return max(n, round(n * fh.size / last)) if last else n, False
    return n, True


def _tiff_quick(tf, nbytes, fh=None):
    """Header-only shape (z, y, x, dtype, note) from the first page, without letting tifffile walk every page's
    IFD (remotely each IFD is one HTTP round trip, i.e. thousands for a stack). None -> use tf.series."""
    p = tf.pages.first if hasattr(tf.pages, "first") else tf.pages[0]
    y, x = p.imagelength, p.imagewidth
    ij = tf.imagej_metadata or {}
    if ij.get("images"):
        return int(ij["images"]), y, x, p.dtype.str, "slices from ImageJ header"
    if p.is_shaped:  # tf.shaped_metadata would walk every page
        try:
            sh = json.loads(p.description).get("shape")
            if sh and len(sh) == 3 and p.samplesperpixel == 1:
                return int(sh[0]), int(sh[1]), int(sh[2]), p.dtype.str, "shape from tifffile header"
        except Exception:
            pass
    comp = getattr(p.compression, "value", p.compression)
    if comp == 1 and nbytes and p.samplesperpixel == 1:
        per = y * x * p.dtype.itemsize
        z = max(1, int(nbytes // per))
        return z, y, x, p.dtype.str, "" if z == 1 else \
            f"z estimated from file size ({nbytes} B / {per} B per uncompressed slice); exact after download"
    if hasattr(fh, "url"):  # remote: count IFDs ourselves, reading the file in big blocks
        z, exact = _count_ifds(fh)
        return z, y, x, p.dtype.str, "" if exact else \
            "z estimated from the spacing of the first pages (file too large to walk remotely); exact after download"
    return None


def read_tiff(f, stats=None, header_only=False, nbytes=None):
    import tifffile
    with tifffile.TiffFile(f) as tf:
        q = _tiff_quick(tf, nbytes or getattr(f, "size", None), f) if header_only else None
        if q:
            z, y, x, dt, note = q
            p = tf.pages.first if hasattr(tf.pages, "first") else tf.pages[0]
            vox, hdr = _tiff_voxel(tf, p)
            return {"shape_zyx": [z, y, x], "dtype": dt, "voxel": vox, "voxel_in_header": hdr,
                    "format": "TIFF" + (f" compression={p.compression.name}" if hasattr(p.compression, "name") else ""),
                    "note": note if z > 1 else ""}
        s = tf.series[0]
        axes, shape = s.axes, s.shape
        dims = dict(zip(axes, shape))
        z = dims.get("Z", 1) * dims.get("I", 1) * dims.get("Q", 1) * dims.get("T", 1)
        y, x = dims.get("Y", shape[-2] if len(shape) >= 2 else 1), dims.get("X", shape[-1])
        ch = dims.get("S", 1) * dims.get("C", 1)
        vox, hdr = _tiff_voxel(tf, tf.pages[0])
        info = {"shape_zyx": [z, y, x], "dtype": s.dtype.str, "voxel": vox, "voxel_in_header": hdr,
                "format": "TIFF" + (f" ({ch} channels)" if ch > 1 else "") + (f" compression={tf.pages[0].compression.name}"
                                                                              if hasattr(tf.pages[0].compression, "name") else "")}
        if stats is not None and not header_only:
            if len(tf.pages) == z and z > 1:
                for p in tf.pages:
                    stats.add(p.asarray())
            else:
                stats.add(s.asarray())
        return info


NIFTI_DT = {2: "u1", 4: "i2", 8: "i4", 16: "f4", 64: "f8", 256: "i1", 512: "u2", 768: "u4", 1024: "i8", 1280: "u8"}


def read_nifti(raw, gz, stats=None, header_only=False):
    f = gzip.GzipFile(fileobj=raw) if gz else raw
    h = f.read(352)
    endian = "<" if struct.unpack("<i", h[:4])[0] == 348 else ">"
    dim = struct.unpack(endian + "8h", h[40:56])
    dtype_code = struct.unpack(endian + "h", h[70:72])[0]
    pixdim = struct.unpack(endian + "8f", h[76:108])
    vox_offset = struct.unpack(endian + "f", h[108:112])[0]
    xyzt = h[123]
    unit = {1: 1e9, 2: 1e6, 3: 1e3}.get(xyzt & 0x07)
    nx, ny, nz = dim[1], dim[2], dim[3] if dim[0] >= 3 else 1
    dt = np.dtype(endian + NIFTI_DT.get(dtype_code, "u1"))
    vox = (pixdim[3] * unit, pixdim[2] * unit, pixdim[1] * unit) if unit else None
    hdr = f"pixdim={pixdim[1]:g},{pixdim[2]:g},{pixdim[3]:g} xyzt_units={xyzt} ({'nm' if unit == 1 else 'um' if unit == 1e3 else 'mm' if unit == 1e6 else 'm' if unit == 1e9 else 'unknown unit'})"
    if (vox is None or not 0.05 < vox[2] < 5000) and 0.05 < pixdim[1] < 5000:
        # nnU-Net style files store the spacing in nm but leave the unit flag at mm (or unset)
        vox = (pixdim[3], pixdim[2], pixdim[1])
        hdr += "; unit flag ignored: values read as nm"
    info = {"shape_zyx": [nz, ny, nx], "dtype": dt.str, "voxel": vox, "voxel_in_header": hdr, "format": "NIfTI-1" + (" (gz)" if gz else "")}
    if stats is not None and not header_only:
        f.read(int(vox_offset) - 352)
        plane = nx * ny * dt.itemsize
        for _ in range(nz):  # NIfTI is x-fastest: one z plane is contiguous
            b = f.read(plane)
            if len(b) < plane:
                break
            stats.add(np.frombuffer(b, dt))
    return info


def read_amira(f, stats=None, header_only=False):
    head = f.read(1 << 16).split(b"# Data section follows")[0].decode("latin-1", "replace")
    m = re.search(r"define Lattice\s+(\d+)\s+(\d+)\s+(\d+)", head)
    x, y, z = map(int, m.groups()) if m else (0, 0, 0)
    bb = re.search(r"BoundingBox\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", head)
    t = re.search(r"Lattice \{\s*(\w+)", head)
    dt = {"byte": "|u1", "ushort": "<u2", "short": "<i2", "float": "<f4", "int": "<i4"}.get(t.group(1) if t else "", "|u1")
    vox, hdr = None, ""
    if bb:
        b = list(map(float, bb.groups()))
        sp = [(b[2 * i + 1] - b[2 * i]) / (n - 1) if n > 1 else 0 for i, n in enumerate((x, y, z))]
        hdr = f"BoundingBox {' '.join(bb.groups())} -> spacing x,y,z = {sp[0]:g},{sp[1]:g},{sp[2]:g} (MIB writes um)"
        if 0.0001 < sp[0] < 1:
            vox = (sp[2] * 1e3 if z > 1 else None, sp[1] * 1e3, sp[0] * 1e3)
    return {"shape_zyx": [z, y, x], "dtype": dt, "voxel": vox, "voxel_in_header": hdr, "format": "Amira mesh",
            "note": "Amira data section not decoded (raw image; statistics not computed)"}


def read_pil(f, stats=None, header_only=False):
    from PIL import Image
    im = Image.open(f)
    w, h = im.size
    dpi = im.info.get("dpi")
    mode_dt = {"L": "|u1", "I;16": "<u2", "I": "<i4", "F": "<f4", "RGB": "|u1", "RGBA": "|u1", "P": "|u1", "1": "|b1"}
    info = {"shape_zyx": [1, h, w], "dtype": mode_dt.get(im.mode, im.mode), "voxel": None,
            "voxel_in_header": f"dpi={dpi}" if dpi else "", "format": f"{im.format} mode={im.mode}"}
    if stats is not None and not header_only:
        a = np.asarray(im)
        stats.add(a if a.ndim == 2 else a[..., 0])
    return info


def read_h5(f, path, stats_factory=None):
    """One item per >=2D dataset inside the HDF5 file."""
    import h5py
    out = []
    with h5py.File(f, "r") as h:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and obj.ndim >= 2:
                shp = list(obj.shape)
                zyx = ([1] + shp)[-3:] if obj.ndim == 2 else shp[-3:]
                attrs = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in obj.attrs.items()}
                res = attrs.get("resolution") or attrs.get("element_size_um")
                vox = None
                if res is not None and len(res) == 3:
                    vox = tuple(float(r) * (1e3 if "element_size_um" in attrs else 1) for r in res)
                info = {"name": name, "shape_zyx": zyx, "dtype": obj.dtype.str, "voxel": vox,
                        "voxel_in_header": f"attrs {json.dumps(attrs, default=str)[:200]}" if attrs else "",
                        "format": "HDF5 dataset"}
                if stats_factory:
                    st = stats_factory(name)
                    for i in range(0, obj.shape[0], 16):
                        st.add(obj[i:i + 16])
                    info["stats"] = st
                out.append(info)
        h.visititems(visit)
    return out


def read_n5_attrs(text):
    a = json.loads(text)
    dims = a["dimensions"][::-1]
    zyx = ([1] * (3 - len(dims)) + dims)[-3:]
    res = a.get("resolution") or (a.get("pixelResolution") or {}).get("dimensions")
    vox = tuple(float(r) for r in res[::-1]) if res and len(res) == 3 else None
    return {"shape_zyx": zyx, "dtype": np.dtype(a["dataType"]).str, "voxel": vox,
            "voxel_in_header": f"resolution={res}" if res else "", "format": f"N5 ({(a.get('compression') or {}).get('type', 'raw')})",
            "block": a.get("blockSize"), "compression": a.get("compression") or {"type": "raw"}, "dims": a["dimensions"],
            "dataType": a["dataType"]}


def n5_stats(info, chunk_entries, stats):
    dt = np.dtype(">" + np.dtype(info["dataType"]).str[1:])
    comp = info["compression"]
    for e in chunk_entries:
        with contextlib.closing(e.opener()) as f:
            b = f.read()
        mode, nd = struct.unpack(">HH", b[:4])
        cdims = struct.unpack(">" + "I" * nd, b[4:4 + 4 * nd])
        off = 4 + 4 * nd + (4 if mode == 1 else 0)
        data = b[off:]
        t = comp.get("type", "raw")
        if t == "gzip":
            data = zlib_decompress(data)
        elif t == "blosc":
            import numcodecs
            data = numcodecs.Blosc().decode(data)
        elif t != "raw":
            raise RuntimeError(f"n5 compression {t} not supported")
        stats.add(np.frombuffer(data, dt)[:int(np.prod(cdims))])


def zlib_decompress(data):
    import zlib
    try:
        return gzip.decompress(data)
    except OSError:
        return zlib.decompress(data)


# ----------------------------------------------------------------------------- grouping into items

def role_of(path):
    p = "/" + path
    if PRED_RX.search(p) and not RAW_DIR_RX.search(p):
        return "prediction"
    if RAW_DIR_RX.search(p):
        return "raw"
    if re.search(r"(error|loss)[-_]?weight", p, re.I):
        return "weights"
    return "label" if LABEL_RX.search(p) else "raw"


def series_key(name):
    """Name with its last digit run replaced: files of one numbered slice series share the key."""
    m = list(re.finditer(r"\d+", name))
    if not m:
        return None, None
    last = m[-1]
    return name[:last.start()] + "#" + name[last.end():], int(last.group())


def group_dir(entry):
    d = os.path.dirname(entry.path)
    # EMPIAR-13156 splits one slice series into numeric range folders ("1-200", "201-400"): group at the parent
    if re.fullmatch(r"\d+-\d+", os.path.basename(d)):
        d = os.path.dirname(d)
    return d


class Builder:
    def __init__(self, meta, remote, sample, manifest):
        self.meta, self.remote, self.sample, self.manifest = meta, remote, sample, manifest
        self.rows = []

    def base(self, did):
        ds = self.manifest.get(did, {})
        m = self.meta.get(did, {})
        return {"dataset_id": did, "dataset": m.get("dataset", ds.get("title", did)),
                "imaging": m.get("imaging", ""), "organism": m.get("organism", ""),
                "cell_type": m.get("organ_tissue_cell_type", ""), "label_classes": ""}

    def emit(self, did, item, role, structure, path, info, n_files=1, extra=None):
        item = item or "(top-level files)"
        r = self.base(did)
        z, y, x = (info.get("shape_zyx") or [None] * 3)
        vox = info.get("voxel")
        vsrc = info.get("voxel_source") or ("file header" if vox else "")
        tv = table_voxel(did, path)
        if vox is None and tv:
            vox, vsrc = tv[:3], tv[3]
        elif vox is not None and tv and tv[1] and abs(vox[2] - tv[2]) / tv[2] > 0.05:
            r["notes"] = f"header pixel {vox[2]:g} nm differs from paper value {tv[2]:g} nm"
        if vox is not None and vox[0] is None and tv and tv[0] and isinstance(z, int) and z > 1:
            vox, vsrc = (tv[0],) + tuple(vox[1:]), f"{vsrc}; z from {tv[3]}"  # e.g. 2D slices stacked into a volume
        if vox is None:
            m = self.meta.get(did, {})
            vsrc = f"see metadata.csv: xy {m.get('xy_resolution_nm', '?')}, z {m.get('z_resolution_nm', '?')}" if m else "not reported"
            vox = (None, None, None)
        vz, vy, vx = vox
        if z == 1 and structure.startswith("2D"):
            vz = None
        dt = np.dtype(info["dtype"]) if info.get("dtype") and info["dtype"][0] in "<>|=" and "/" not in info["dtype"] else None
        nvox = z * y * x if all(isinstance(v, int) for v in (z, y, x)) else None
        r.update({"item": item, "role": role, "structure": structure, "path": f"{did}/{path}",
                  "source_url": info.get("url", ""), "n_files": n_files, "shape_z": z, "shape_y": y, "shape_x": x,
                  "dtype": info.get("dtype", ""), "bits": dt.itemsize * 8 if dt is not None else "",
                  "voxel_z_nm": _r(vz), "voxel_y_nm": _r(vy), "voxel_x_nm": _r(vx), "voxel_source": vsrc,
                  "voxel_in_header": info.get("voxel_in_header", ""),
                  "physical_z_um": _r(z * vz / 1e3) if vz and isinstance(z, int) else "",
                  "physical_y_um": _r(y * vy / 1e3) if vy and isinstance(y, int) else "",
                  "physical_x_um": _r(x * vx / 1e3) if vx and isinstance(x, int) else "",
                  "n_voxels": nvox or "", "bytes_uncompressed": nvox * dt.itemsize if nvox and dt is not None else "",
                  "format": info.get("format", ""), "stats_basis": info.get("stats_basis", "")})
        if info.get("note"):
            r["notes"] = "; ".join(filter(None, [r.get("notes"), info["note"]]))
        if extra:
            r.update({k: v for k, v in extra.items() if v not in (None, "")})
        self.rows.append(r)

    # -- files of a downloaded (or remote) dataset
    def files(self, did, entries):
        archives = [e for e in entries if e.opener is None]
        entries = [e for e in entries if e.opener is not None]
        for a in archives:
            if not any(e.path.startswith(a.path + "!/") for e in entries):
                self.emit(did, os.path.basename(a.path), "archive", "archive (contents not read)", a.path,
                          {"format": os.path.splitext(a.path)[1], "url": a.url,
                           "note": "contents read after download" if self.remote else "could not open archive"},
                          extra={"bytes_on_disk": a.size})
        # N5 containers (Platynereis training data): dataset = folder with attributes.json holding "dimensions"
        n5_attr = [e for e in entries if e.path.endswith("attributes.json") and ".n5" in e.path]
        n5_done = set()
        for e in n5_attr:
            with contextlib.closing(e.opener()) as f:
                txt = f.read().decode()
            if '"dimensions"' not in txt:
                continue
            dpath = os.path.dirname(e.path)
            info = read_n5_attrs(txt)
            chunks = [c for c in entries if c.path.startswith(dpath + "/") and re.fullmatch(r"(\d+/)*\d+", c.path[len(dpath) + 1:])]
            n5_done.update(c.path for c in chunks)
            role = role_of(dpath)
            st = Stats(role == "label")
            if not self.remote:
                n5_stats(info, chunks, st)
                info["stats_basis"] = "all chunks"
            self.emit(did, dpath.split("!/")[-1], role, "3D volume (N5)", dpath, info, len(chunks),
                      extra=dict(st.row(), bytes_on_disk=sum(c.size or 0 for c in chunks)))
        entries = [e for e in entries if e.path not in n5_done and not (".n5/" in e.path and e.path.endswith(".json"))]

        groups = collections.defaultdict(list)
        jsons = collections.defaultdict(list)
        others = []
        for e in entries:
            if e.ext in IMG_EXT:
                groups[group_dir(e)].append(e)
            elif e.ext == ".json":
                jsons[group_dir(e)].append(e)
            else:
                others.append(e)
        for g, es in sorted(jsons.items()):
            if not self.labelme(did, g, es):
                others += es
        for g, es in sorted(groups.items()):
            self.image_group(did, g, es)
        if others:
            ext = collections.Counter(e.ext or "(none)" for e in others)
            self.emit(did, "other files", "other", "non-image files", "(various)",
                      {"format": ", ".join(f"{k} x{v}" for k, v in ext.most_common())},
                      n_files=len(others), extra={"bytes_on_disk": sum(e.size or 0 for e in others),
                                                  "notes": "e.g. " + ", ".join(os.path.basename(e.path) for e in others[:5])})
        meta = [e for e in others if os.path.basename(e.path).lower() == "cem_mitolab_metadata.xlsx"]
        if meta:
            try:
                with contextlib.closing(meta[0].opener()) as f:
                    cem_voxels([r for r in self.rows if r["dataset_id"] == did], f.read())
            except Exception as ex:
                log.warning("%s: could not apply %s: %r", did, meta[0].path, ex)

    def labelme(self, did, d, es):
        classes = collections.Counter()
        n_ann, sizes = 0, []
        for e in es:
            try:
                with contextlib.closing(e.opener()) as f:
                    j = json.loads(f.read())
            except Exception:
                continue
            if not isinstance(j, dict) or "shapes" not in j:
                continue
            n_ann += 1
            classes.update(s.get("label", "?") for s in j["shapes"])
            if j.get("imageHeight"):
                sizes.append((j["imageHeight"], j["imageWidth"]))
        if not n_ann:
            return False
        ys, xs = [s[0] for s in sizes] or [None], [s[1] for s in sizes] or [None]
        info = {"shape_zyx": [1, f"{min(ys)}-{max(ys)}" if min(ys) != max(ys) else ys[0],
                              f"{min(xs)}-{max(xs)}" if min(xs) != max(xs) else xs[0]],
                "format": "Labelme JSON (polygons)", "stats_basis": f"all {n_ann} annotation files"}
        self.emit(did, d.split("!/")[-1] + " (Labelme)", "label", "2D polygon annotations", d, info, n_files=len(es),
                  extra={"n_images": n_ann, "n_objects": sum(classes.values()),
                         "label_classes": "; ".join(f"{k} ({v})" for k, v in classes.most_common()),
                         "bytes_on_disk": sum(e.size or 0 for e in es)})
        return True

    def read(self, e, stats=None, header_only=False):
        ext = e.ext
        if ext in (".nii", ".nii.gz"):
            with contextlib.closing(e.opener()) as f:
                return read_nifti(f, ext == ".nii.gz", stats, header_only)
        if ext == ".am":
            with contextlib.closing(e.opener()) as f:
                return read_amira(f)
        if ext == ".dm4":
            return {"format": "Gatan DM4", "note": "DM4 not parsed (the same slices are also provided as processed TIFF)"}
        if self.remote and "!/" in e.path and (e.size or 0) > (4 << 20):
            # a compressed zip member cannot be read at random offsets remotely: parse the header from its first 2 MiB
            with contextlib.closing(e.opener()) as f:
                buf = f.read(2 << 20)
            try:
                info = read_tiff(io.BytesIO(buf), header_only=True, nbytes=e.size) if ext in (".tif", ".tiff") else \
                    read_pil(io.BytesIO(buf), header_only=True)
                info["note"] = "; ".join(filter(None, ["header read from the first 2 MiB of the zipped file",
                                                       info.get("note")]))
                return info
            except Exception:
                return {"format": ext.upper()[1:], "note": "header not in the first 2 MiB of the zipped file; read after download"}
        with seekable(e) as f:
            if ext in (".tif", ".tiff"):
                return read_tiff(f, stats, header_only)
            if ext in (".png", ".jpg", ".jpeg"):
                return read_pil(f, stats, header_only)
        return {"format": ext}

    def image_group(self, did, d, es):
        es = sorted(es, key=lambda e: e.path)
        if any(e.ext in (".h5", ".hdf5") for e in es):
            for e in [e for e in es if e.ext in (".h5", ".hdf5")]:
                if self.remote:
                    self.emit(did, os.path.basename(e.path), role_of(e.path), "HDF5 file (read after download)", e.path,
                              {"format": "HDF5", "url": e.url}, extra={"bytes_on_disk": e.size})
                    continue
                with seekable(e, max_mem=0) as f:
                    for info in read_h5(f, e.path, lambda name: Stats(role_of("/" + name) == "label")):
                        st = info.pop("stats")
                        info["stats_basis"] = "all voxels"
                        self.emit(did, f"{os.path.basename(e.path)}:{info['name']}", role_of("/" + info["name"]),
                                  "3D volume (HDF5)", f"{e.path}:{info['name']}", info, extra=dict(st.row(), bytes_on_disk=e.size))
            es = [e for e in es if e.ext not in (".h5", ".hdf5")]
            if not es:
                return
        # split by numbered-series key; a key shared by >= 10 files with one shape = slice series
        by_key = collections.defaultdict(list)
        for e in es:
            k, n = series_key(os.path.basename(e.path))
            by_key[(k, e.ext) if k else (e.path, e.ext)].append((n, e))
        singles = []
        for (k, ext), lst in sorted(by_key.items(), key=lambda kv: str(kv[0])):
            if len(lst) >= 10 and ext not in (".dm4", ".nii", ".nii.gz"):
                try:  # a slice series is made of 2D files; numbered 3D files stay separate items
                    first = self.read(sorted(lst, key=lambda t: t[0])[0][1], header_only=True)
                    if (first.get("shape_zyx") or [1])[0] > 1:
                        singles += [e for _, e in lst]
                        continue
                except Exception:
                    pass
                self.slice_series(did, d, k, sorted(lst, key=lambda t: t[0]))
            elif len(lst) >= 10 and ext == ".dm4":
                lst = sorted(lst, key=lambda t: t[0])
                self.emit(did, f"{os.path.basename(d)}/{k}", role_of(lst[0][1].path), "slice series (DM4, not parsed)",
                          f"{d}/{k}", {"format": "Gatan DM4", "shape_zyx": [len(lst), None, None],
                                       "note": "DM4 originals; the same slices are also provided as processed TIFF"},
                          n_files=len(lst), extra={"bytes_on_disk": sum(e.size or 0 for _, e in lst)})
            else:
                singles += [e for _, e in lst]
        if not singles:
            return
        two_d = []
        keep = {id(e) for e in singles[:: max(1, len(singles) // self.sample)]} if self.remote else None
        for e in singles:
            if keep is not None and id(e) not in keep and e.ext not in (".nii", ".nii.gz", ".am", ".mrc"):
                two_d.append((e, None))
                continue
            role = role_of(e.path)
            st = Stats(role == "label")
            try:
                info = self.read(e, None if self.remote else st)
            except Exception as ex:
                log.warning("cannot read %s: %r", e.path, ex)
                info = {"format": e.ext, "note": f"unreadable: {ex!r}"[:200]}
            info["url"] = e.url
            if (info.get("shape_zyx") or [1])[0] and (info.get("shape_zyx") or [1])[0] > 1:
                info["stats_basis"] = "header only" if self.remote else "all voxels"
                self.emit(did, os.path.basename(e.path), role, "3D volume", e.path, info,
                          extra=dict(st.row(), bytes_on_disk=e.size))
            else:
                two_d.append((e, (info, st)))
        by_role = collections.defaultdict(list)  # a folder with image + label files -> one row per role
        for e, x in two_d:
            by_role[role_of(e.path)].append((e, x))
        for _, lst in sorted(by_role.items()):
            self.image_set(did, d, lst)

    def slice_series(self, did, d, key, lst):
        es = [e for _, e in lst]
        role = role_of(es[0].path)
        st = Stats(role == "label")
        if self.remote:
            pick = sorted({0, len(es) // 2, len(es) - 1})
            infos = [self.read(es[i], header_only=True) for i in pick]
            basis = f"headers of slices {', '.join(str(lst[i][0]) for i in pick)}"
        else:
            infos = [self.read(e, st) for e in es]
            basis = "all slices"
        good = [i for i in infos if i.get("shape_zyx")]
        shapes = collections.Counter(tuple(i["shape_zyx"][1:]) for i in good)
        (y, x), _ = shapes.most_common(1)[0] if shapes else ((None, None), 0)
        nums = [n for n, _ in lst]
        gaps = sorted(set(range(nums[0], nums[-1] + 1)) - set(nums))
        info = dict((good or infos)[0], shape_zyx=[len(es), y, x], stats_basis=basis)
        notes = [] if good else [infos[0].get("note") or "slice headers not readable remotely; read after download"]
        if len(shapes) > 1:
            notes.append(f"slice shapes differ: {dict((f'{a}x{b}', c) for (a, b), c in shapes.items())}")
        if gaps:
            notes.append(f"{len(gaps)} missing slice numbers, e.g. {gaps[:5]}")
        info["note"] = "; ".join(notes)
        self.emit(did, f"{os.path.basename(d) or d}/{key}", role, "3D volume (slice series)", f"{d}/{key}", info,
                  n_files=len(es), extra=dict(st.row(), bytes_on_disk=sum(e.size or 0 for e in es),
                                               region_offset_zyx_vox=f"slices {nums[0]}-{nums[-1]}"))

    def image_set(self, did, d, two_d):
        read = [(e, x) for e, x in two_d if x is not None]
        role = collections.Counter(role_of(e.path) for e, _ in two_d).most_common(1)[0][0]
        st = Stats(role == "label")
        shapes, dts, vox, hdrs, fmts = [], collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
        for e, (info, s) in read:
            sh = info.get("shape_zyx")
            if sh:
                shapes.append(tuple(sh[1:]))
            dts[info.get("dtype", "")] += 1
            fmts[info.get("format", "")] += 1
            if info.get("voxel"):
                vox[tuple(round(v, 3) if v else v for v in info["voxel"])] += 1
            if info.get("voxel_in_header"):
                hdrs[info["voxel_in_header"]] += 1
            if s.n:
                st.n += s.n; st.nz += s.nz; st.vals |= s.vals
                if not s.label and s.mn is not None:
                    st.mn = s.mn if st.mn is None else min(st.mn, s.mn)
                    st.mx = s.mx if st.mx is None else max(st.mx, s.mx)
                    st.s += s.s; st.s2 += s.s2
        ys = [a for a, _ in shapes] or [None]
        xs = [b for _, b in shapes] or [None]
        rng = lambda v: (f"{min(v)}-{max(v)}" if min(v) != max(v) else v[0]) if v[0] is not None else ""
        info = {"shape_zyx": [1, rng(ys), rng(xs)], "dtype": "/".join(k for k in dts if k),
                "voxel": vox.most_common(1)[0][0] if vox else None,
                "voxel_in_header": hdrs.most_common(1)[0][0] if hdrs else "",
                "format": "; ".join(k for k in fmts if k),
                "stats_basis": (f"headers of {len(read)} of {len(two_d)} images (sampled)" if self.remote and len(read) < len(two_d)
                                else "headers only" if self.remote else f"all {len(two_d)} images")}
        if len(vox) > 1:
            info["note"] = f"pixel sizes differ: {dict((str(k), c) for k, c in vox.most_common(5))}"
        med = f"{int(np.median(ys))}x{int(np.median(xs))}" if ys[0] is not None else ""
        name = d.split("!/")[-1] or d
        if len(two_d) == 1:
            e = two_d[0][0]
            self.emit(did, os.path.basename(e.path), role, "2D image", e.path, dict(info, url=e.url),
                      extra=dict(st.row(), bytes_on_disk=e.size))
        else:
            self.emit(did, name, role, "2D image set", d, info, n_files=len(two_d),
                      extra=dict(st.row(), n_images=len(two_d), shape_yx_median=med,
                                 bytes_on_disk=sum(e.size or 0 for e, _ in two_d)))

    # -- crops
    def crop_remote(self, did, crop):
        for s in crop["sources"]:
            try:
                src = F.Source.make(s, 8)
                off = list(s.get("offset_zyx", crop["offset_zyx"]))
                size = list(s.get("size_zyx", crop["size_zyx"]))
                size = [max(0, min(a, n - o)) for o, a, n in zip(off, size, src.shape_zyx)]
                info = {"shape_zyx": size, "dtype": np.dtype(src.dtype).str,
                        "voxel": tuple(s.get("voxel_nm_zyx", crop["voxel_nm_zyx"])), "format": "OME-Zarr (written by fetch_em)",
                        "voxel_source": "repository metadata (source array .zattrs)" if s["type"] == "zarr2" and "cosem" in s.get("url", "") else "manifest (paper / repository entry)",
                        "url": s.get("url") or s.get("url_template") or s.get("zip_url"), "stats_basis": "planned (source headers)"}
            except Exception as e:
                info = {"format": "OME-Zarr", "note": f"source not readable now: {e!r}"[:200]}
                off = s.get("offset_zyx", crop["offset_zyx"])
            self.crop_row(did, crop, s, off, info, Stats(False))

    def crop_local(self, did, root, crop_json):
        import zarr
        side = json.load(open(crop_json))
        zpath = crop_json[:-5] + ".ome.zarr"
        g = zarr.open_group(zpath, mode="r")
        crop = {"name": side["crop"], **{k: side.get(k) for k in ("note", "labels_note", "voxel_nm_zyx")}}
        for aname, a in side["arrays"].items():
            arr = g[aname + "/s0"]
            role = "label" if aname.startswith("labels/") else "raw"
            st = Stats(role == "label")
            for z in range(0, arr.shape[0], 16):
                st.add(arr[z:z + 16])
            info = {"shape_zyx": list(arr.shape), "dtype": np.dtype(arr.dtype).str, "voxel": tuple(a["voxel_nm_zyx"]),
                    "format": "OME-Zarr v0.4 (zarr 2, blosc zstd)", "stats_basis": "all voxels",
                    "voxel_source": "repository metadata (source array .zattrs)" if "cosem" in json.dumps(a["source"]) else "manifest (paper / repository entry)",
                    "url": a["source"].get("url") or a["source"].get("url_template") or a["source"].get("zip_url")}
            if a.get("missing_source_slices"):
                info["note"] = f"{len(a['missing_source_slices'])} source slices missing (written as zeros)"
            du = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(os.path.join(zpath, aname)) for f in fs)
            self.crop_row(did, crop, dict(a["source"], name=aname), a["offset_vox_zyx"], info, st, du,
                          os.path.relpath(os.path.join(zpath, aname), root).replace(os.sep, "/"))

    def crop_row(self, did, crop, s, off, info, st, du="", path=None):
        aname = s["name"]
        role = "label" if aname.startswith("labels/") else "raw"
        notes = [n for n in (s.get("note"), crop.get("labels_note") if role == "label" else None, info.get("note")) if n]
        info["note"] = "; ".join(notes)
        whole = s.get("size_zyx") is not None and s["size_zyx"][0] >= 10 ** 6
        region = ("whole array (small annotated block)" if whole else str(list(off)))
        if s.get("world_offset_vox_zyx") and whole:
            region += f" at world voxel {s['world_offset_vox_zyx']}"
        self.emit(did, f"{crop['name']}/{aname}", role, "3D crop", path or f"{crop['name']}.ome.zarr/{aname}", info,
                  extra=dict(st.row(), region_offset_zyx_vox=region, bytes_on_disk=du,
                             label_classes=aname[7:] if role == "label" else ""))


def check_pairs(rows):
    """Flag label volumes whose shape differs from the matching raw volume (nnU-Net naming: case_0000 = raw)."""
    raw = {}
    for r in rows:
        if r["role"] == "raw" and r["structure"] == "3D volume":
            raw[re.sub(r"_0000(\.nii(\.gz)?|\.tiff?)$", r"\1", os.path.basename(r["path"]))] = r
    for r in rows:
        if r["role"] == "label" and r["structure"] == "3D volume":
            m = raw.get(os.path.basename(r["path"]))
            if m and (m["shape_z"], m["shape_y"], m["shape_x"]) != (r["shape_z"], r["shape_y"], r["shape_x"]):
                r["notes"] = "; ".join(filter(None, [r.get("notes"), f"shape differs from raw {m['item']} "
                                                     f"({m['shape_z']}x{m['shape_y']}x{m['shape_x']}): axes may be transposed"]))


def xlsx_rows(data, sheet):
    """Rows of one sheet of an .xlsx file as {column letter: text} (no openpyxl needed)."""
    import xml.etree.ElementTree as ET
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(data))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = ["".join(t.text or "" for t in si.iter(m + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(m + "si")]
    for row in ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml")).iter(m + "row"):
        out = {}
        for c in row.findall(m + "c"):
            v, t = c.find(m + "v"), c.find(m + "is/" + m + "t")
            val = v.text if v is not None else (t.text if t is not None else "")
            out[re.match(r"[A-Z]+", c.get("r")).group()] = ss[int(val)] if c.get("t") == "s" else (val or "")
        yield out


def cem_voxels(rows, xlsx):
    """CEM-MitoLab: one folder per source (Sample UID). Pixel size per source from cem_mitolab_metadata.xlsx
    (sheet 2): source pixel size (AD/AE/AF, unit AG) x the bin MitoLab applied (AK = bin XY, AL = bin Z)."""
    meta = {}
    for r in xlsx_rows(xlsx, 2):
        try:
            meta[r.get("B", "").strip()] = r
        except Exception:
            pass
    hit = miss = 0
    for r in rows:
        m = re.search(r"cem_mitolab/([^/]+)", r["path"] or r["item"])
        if not m or r["role"] not in ("raw", "label"):
            continue
        s = meta.get(m.group(1))
        if not s:
            miss += 1
            continue
        for col, k in (("I", "organism"), ("L", "cell_type"), ("E", "imaging")):
            if (s.get(col) or "").strip():
                r[k] = s[col].strip()
        if not (s.get("L") or "").strip() and (s.get("H") or "").strip():
            r["cell_type"] = s["H"].strip()
        if not (s.get("AD") or "").strip():
            r["voxel_source"] = "not reported (blank in cem_mitolab_metadata.xlsx for this source)"
            miss += 1
            continue
        unit = (s.get("AG") or "").strip().lower()
        f = {"nm": 1, "um": 1e3, "µm": 1e3}.get(unit, 1)
        try:
            bxy = float(s.get("AK") or 1)
            bz = float(s.get("AL") or 1)
            vx, vy = float(s["AD"]) * f * bxy, float(s.get("AE") or s["AD"]) * f * bxy
            vz = float(s["AF"]) * f * bz if s.get("F", "").strip().upper() == "3D" and s.get("AF") else None
        except ValueError:
            miss += 1
            continue
        r["voxel_x_nm"], r["voxel_y_nm"], r["voxel_z_nm"] = _r(vx), _r(vy), _r(vz)
        for a, v in (("x", vx), ("y", vy), ("z", vz)):
            sh = str(r[f"shape_{a}"])
            r[f"physical_{a}_um"] = _r(int(sh) * v / 1e3) if v and sh.isdigit() else ""
        r["voxel_source"] = "cem_mitolab_metadata.xlsx: source pixel size x MitoLab bin" + \
            ("" if unit else " (unit not stated, assumed nm)")
        r["voxel_in_header"] = "; ".join(filter(None, [
            r.get("voxel_in_header"), f"xlsx: pixel {s.get('AD')}/{s.get('AE')}/{s.get('AF')} {s.get('AG') or '?'}, "
            f"bin xy {s.get('AK') or 1} z {s.get('AL') or 1}, {s.get('E', '')} {s.get('F', '')}, source: {s.get('H', '')}"]))
        hit += 1
    log.info("cem_mitolab_metadata.xlsx: voxel size set for %d rows, %d rows without a usable entry", hit, miss)


def bia_file_lists(rows, ds):
    """BioImage Archive studies (S-BIAD...): per-file pixel size, organism, cell type and imaging method from the
    study's file lists; a label gets its source image's values plus its own annotation overview."""
    import fetch_em
    acc = ds["id"].upper()
    url0 = next((f["url"] for f in ds.get("files", []) if "/Files/" in f.get("url", "")), "")
    if not url0:
        return
    base = url0.split("/Files/")[0] + "/Files/"
    study = fetch_em.session().get(f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}", timeout=60).json()
    names = set()

    def walk(s):
        for a in s.get("attributes", []):
            if a.get("name") == "File List" and a.get("value"):
                names.add(a["value"])
        for sub in s.get("subsections", []):
            for x in (sub if isinstance(sub, list) else [sub]):
                walk(x)
    walk(study["section"])
    attrs = {}
    for n in sorted(names):
        for f in fetch_em.session().get(base + n, timeout=60).json():
            attrs[f["path"]] = {a["name"]: a.get("value") for a in f.get("attributes", [])}

    def nm(v):
        m = re.match(r"\s*([\d.]+)\s*(nm|µm|um)?", v or "")
        return float(m.group(1)) * (1e3 if m.group(2) in ("µm", "um") else 1) if m else None
    hit = 0
    for r in rows:
        p = (r["path"] or "").split("!/")[-1]
        a = next((v for k, v in attrs.items() if p.endswith(k)), None)
        if a is None:
            continue
        src = attrs.get(a.get("source_image") or "", a)
        vx, vy, vz = (nm(src.get(f"single_voxel_physical_size_{k}")) for k in "xyz")
        if vx:
            r["voxel_x_nm"], r["voxel_y_nm"], r["voxel_z_nm"] = _r(vx), _r(vy or vx), _r(vz)
            for ax, v in (("x", vx), ("y", vy or vx), ("z", vz)):
                sh = str(r[f"shape_{ax}"])
                r[f"physical_{ax}_um"] = _r(int(sh) * v / 1e3) if v and sh.isdigit() else ""
            r["voxel_source"] = f"{acc} file list ({'source image ' + a['source_image'] if 'source_image' in a else 'this file'})"
        for k, col in (("organism", "organism"), ("cell_type", "cell_type"), ("imaging_method", "imaging")):
            if src.get(k):
                r[col] = src[k]
        if a.get("annotation_overview"):
            r["notes"] = "; ".join(filter(None, [r.get("notes"), a["annotation_overview"]]))
        hit += 1
    log.info("%s file lists (%s): %d rows annotated", acc, ", ".join(sorted(names)), hit)


def voxel_from_raw(rows):
    """Labels with no voxel size get it from the dataset's raw, when all raw rows that have one agree
    (mask TIFFs often carry no pixel size)."""
    keys = ("voxel_z_nm", "voxel_y_nm", "voxel_x_nm")
    src = {tuple(r[k] for k in keys): r for r in rows
           if r["role"] == "raw" and (r["voxel_y_nm"] or r["voxel_x_nm"])}
    if len(src) != 1:
        return
    (vox, m), = src.items()
    for r in rows:
        if r["role"] == "label" and not (r["voxel_y_nm"] or r["voxel_x_nm"]):
            for k, v in zip(keys, vox):
                r[k] = v
            for a in "zyx":
                s, v = r[f"shape_{a}"], r[f"voxel_{a}_nm"]
                if v != "" and str(s).isdigit():
                    r[f"physical_{a}_um"] = _r(int(s) * float(v) / 1e3)
            r["voxel_source"] = f"copied from raw {m['item'] or m['path']} (label file has no pixel size)"
            if "pixel sizes differ" in (m.get("notes") or ""):
                r["notes"] = "; ".join(filter(None, [r.get("notes"), "raw images have different pixel sizes "
                                                     "(see raw row); each mask has its own image's size"]))


def _r(v):
    return "" if v is None else round(float(v), 4)


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="download root (as given to fetch_em.py --out)")
    ap.add_argument("--remote", action="store_true", help="no download: read headers over HTTP")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--manifest", default=os.path.join(HERE, "datasets.json"))
    ap.add_argument("--metadata", default=os.path.join(HERE, "..", "metadata.csv"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--sample", type=int, default=30, help="--remote: images read per 2D image set")
    args = ap.parse_args()
    if not args.remote and not args.out:
        ap.error("--out is required unless --remote")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("fetch_em").setLevel(logging.WARNING)
    man = json.load(open(args.manifest, encoding="utf-8"))["datasets"]
    manifest = {d["id"]: d for d in man}
    b = Builder(load_meta(args.metadata), args.remote, args.sample, manifest)
    for ds in man:
        did = ds["id"]
        if args.only and did not in args.only:
            continue
        log.info("=== %s", did)
        n0 = len(b.rows)
        try:
            if ds["kind"] == "manual":
                b.emit(did, did, "raw+label", "manual download", did,
                       {"format": "see MANUAL_DOWNLOAD.txt", "note": "not downloaded by script"})
                if not args.remote and os.path.isdir(os.path.join(args.out, did)):
                    b.files(did, local_entries(did, os.path.join(args.out, did)))
            elif ds["kind"] == "crops":
                if args.remote:
                    for c in ds["crops"]:
                        b.crop_remote(did, c)
                else:
                    root = os.path.join(args.out, did)
                    for fn in sorted(os.listdir(root)):
                        if fn.endswith(".json") and os.path.isdir(os.path.join(root, fn[:-5] + ".ome.zarr")):
                            b.crop_local(did, root, os.path.join(root, fn))
                    b.files(did, local_entries(did, root))
            elif args.remote:
                if ds["kind"] == "gdown_folder":
                    b.emit(did, ds.get("dest", did), "raw+label", "Google Drive folder (read after download)", did,
                           {"format": "folder", "url": ds["url"]}, n_files=ds.get("expect_files", ""))
                else:
                    b.files(did, remote_entries(ds))
            else:
                b.files(did, local_entries(did, os.path.join(args.out, did)))
        except Exception as e:
            log.exception("%s failed", did)
            b.emit(did, did, "error", "inventory failed", did, {"note": repr(e)[:300]})
        check_pairs(b.rows[n0:])
        if did.startswith("s-biad"):
            try:
                bia_file_lists(b.rows[n0:], ds)
            except Exception as ex:
                log.warning("%s: BioImage Archive file lists not read: %r", did, ex)
        voxel_from_raw(b.rows[n0:])
        log.info("%s: %d items", did, len(b.rows) - n0)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(b.rows)
    log.info("wrote %s: %d rows", args.csv, len(b.rows))


if __name__ == "__main__":
    sys.exit(main())
