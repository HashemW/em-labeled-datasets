#!/usr/bin/env python3
"""Download / crop public EM datasets described in a JSON manifest (datasets.json).

Usage
-----
  python fetch_em.py --list                                  # show dataset ids
  python fetch_em.py --out /scratch/em --only empiar-11037   # one dataset
  python fetch_em.py --out /scratch/em --task-index 3        # SLURM array task -> 4th dataset
  python fetch_em.py --out /tmp/emtest --test                # smoke-test every route (tiny reads)

Dataset kinds (manifest "kind")
-------------------------------
  files         explicit list of URLs (+ optional md5/size), downloaded with resume + verification
  http_dir      crawl an Apache-style directory listing (EMPIAR / EBI FTP over HTTPS), with include/exclude regexes
  gdown_folder  public Google Drive folder via gdown
  manual        cannot be scripted; writes MANUAL_DOWNLOAD.txt with instructions
  crops         read sub-volumes from remote zarr v2 / N5 / TIFF-slice / zipped-TIFF-slice sources and write
                each crop as OME-Zarr (v0.4, zarr format 2) + a JSON sidecar

Everything is resumable: completed files are skipped, partial files continue via HTTP Range,
crops record finished z-slabs in <crop>.zarr/.progress.json.
Each dataset writes <out>/<id>/_status.json; the process exits non-zero if anything failed.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
import zipfile
import zlib
from html import unescape
from urllib.parse import unquote, urljoin

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("fetch_em")
UA = "fetch_em/1.0 (research dataset download; python-requests)"
CHUNK = 1 << 20


# ----------------------------------------------------------------------------- HTTP helpers

_tls = threading.local()
MAX_RETRIES = 10  # lowered in --test mode


class CappedRetry(Retry):
    """Honour Retry-After, but never wait longer than 2 minutes per attempt (4TU sends Retry-After: 3600)."""

    def get_retry_after(self, response):
        ra = super().get_retry_after(response)
        return None if ra is None else min(ra, 120.0)


def session():
    """Thread-local requests session with retries on transient errors."""
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        retry = CappedRetry(total=MAX_RETRIES, connect=MAX_RETRIES, read=MAX_RETRIES, backoff_factor=2,
                            status_forcelist=(429, 500, 502, 503, 504),
                            allowed_methods=frozenset(["GET", "HEAD"]), respect_retry_after_header=True)
        ad = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        s.mount("https://", ad)
        s.mount("http://", ad)
        s.headers["User-Agent"] = UA
        _tls.s = s
    return s


def with_retries(fn, what, tries=6):
    """Retry fn() on network errors that urllib3's Retry does not cover (e.g. reset mid-body)."""
    for i in range(tries):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                zlib.error, EOFError) as e:
            if i == tries - 1:
                raise
            wait = min(120, 5 * 2 ** i)
            log.warning("%s: %s (%.150s); retry %d/%d in %ds", what, e.__class__.__name__, e, i + 1, tries - 1, wait)
            time.sleep(wait)


def _check_not_html(r, url):
    """EBI under load answers some requests with 200 and an HTML page instead of the file (a TIFF read as
    b'<!DO', 2026-10-15). No data URL in the manifest is HTML, so treat that as a transient error and retry."""
    if "text/html" in r.headers.get("Content-Type", "") and not url.endswith(("/", ".html", ".htm")):
        raise requests.ConnectionError(f"HTML page instead of data (HTTP {r.status_code}): {url}")


def http_get_bytes(url, start=None, end=None, missing_ok=False, verify=False):
    """GET a URL (optionally a byte range [start, end] inclusive). Returns None for 404 if missing_ok.
    verify=True: read until two copies are identical (for payloads without any checksum)."""
    def go():
        h = {}
        if start is not None:
            h["Range"] = f"bytes={start}-{'' if end is None else end}"
        r = session().get(url, headers=h, timeout=(30, 300))
        if missing_ok and r.status_code in (404, 403):
            return None
        r.raise_for_status()
        _check_not_html(r, url)
        cl = r.headers.get("Content-Length")
        if cl is not None and "Content-Encoding" not in r.headers and len(r.content) != int(cl):
            raise requests.ConnectionError(f"truncated body {len(r.content)} != {cl}")
        if start is not None and end is not None and r.status_code == 206 and len(r.content) != end - start + 1:
            raise requests.ConnectionError(f"short range {len(r.content)} != {end - start + 1}")
        if start is not None and r.status_code != 206:
            # server ignored Range: slice locally
            return r.content[start: None if end is None else end + 1]
        return r.content
    if not verify:
        return with_retries(go, url)
    # Integrity by agreement: under heavy load we observed (2026-10-08) responses with the right length but
    # different bytes. For data without any checksum/CRC, read until two copies agree.
    seen = {}
    for _ in range(5):
        b = with_retries(go, url)
        if b is None:
            return None
        h = hashlib.md5(b).digest()
        if h in seen:
            if len(seen) > 1:
                log.warning("integrity: %s [%s-%s] differed between reads; using the majority copy", url, start, end)
            return b
        seen[h] = b
    raise RuntimeError(f"integrity: no two identical reads of {url} [{start}-{end}] in 5 tries")


class HTTPFile(io.RawIOBase):
    """Read-only seekable file over HTTP Range requests (for zipfile / tifffile)."""

    def __init__(self, url, size=None, verify=False):
        self.url, self.pos, self.verify = url, 0, verify
        if size is None:
            # EBI under load sometimes answers HEAD without Content-Length (KeyError on EMPIAR-13420,
            # 2026-10-14): fall back to a Range probe, and retry if the server still gives no size.
            def go():
                s = remote_size(url)
                if s is None:
                    raise requests.ConnectionError(f"server gave no size for {url}")
                return s
            size = with_retries(go, url)
            if size < 0:
                raise FileNotFoundError(url)
        self.size = size

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        d = http_get_bytes(self.url, self.pos, self.pos + n - 1, verify=self.verify)
        self.pos += len(d)
        return d

    def readinto(self, b):
        d = self.read(len(b))
        b[: len(d)] = d
        return len(d)


def remote_size(url):
    """Size of a remote file in bytes; -1 if it does not exist, None if the server will not say."""
    def go():
        h = {"Accept-Encoding": "identity"}  # with gzip allowed, Dropbox drops Content-Length
        r = session().head(url, headers=h, allow_redirects=True, timeout=(30, 60))
        if r.status_code == 404:
            return -1
        r.raise_for_status()
        _check_not_html(r, url)
        if r.headers.get("Content-Length") is not None:
            return int(r.headers["Content-Length"])
        with session().get(url, headers={**h, "Range": "bytes=0-0"}, stream=True,
                           allow_redirects=True, timeout=(30, 60)) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.rsplit("/", 1)[1]) if r.status_code == 206 and cr[-1:].isdigit() else None
    return with_retries(go, url)


def download_file(url, dest, md5=None, size=None, test=False, _redo=False):
    """Resumable download with size/md5 verification. In test mode only fetch 1 MiB unless file is small."""
    if size is None and not test:
        # No size in the manifest (e.g. http_dir listings): take the server's. A short body without a
        # Content-Length once slipped through as a 994-byte copy of a 33366-byte TIFF (EMPIAR-10994, 2026-10-14),
        # and an existing file of the wrong size is fetched again instead of being skipped.
        try:
            s = remote_size(url)
            size = s if s and s > 0 else None
        except Exception as e:
            log.warning("could not get the server size of %s (%r); size not checked", url, e)
    if os.path.exists(dest):
        if size is None or os.path.getsize(dest) == size:
            log.info("exists, skip: %s", dest)
            return "skipped"
        log.warning("size mismatch for existing %s (%d != %d): re-downloading", dest, os.path.getsize(dest), size)
        os.remove(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if test:
        return _test_download(url, dest, md5, size)
    part = dest + ".part"
    for attempt in range(8):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        h = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session().get(url, headers=h, stream=True, timeout=(30, 300), allow_redirects=True) as r:
                if r.status_code == 416:  # already complete
                    break
                r.raise_for_status()
                _check_not_html(r, url)
                if have and r.status_code != 206:
                    log.warning("server does not support resume; restarting %s", url)
                    have = 0
                mode = "ab" if have else "wb"
                total = r.headers.get("Content-Length")
                total = int(total) + have if total is not None else None
                if size is not None and total is not None and total != size:
                    log.warning("%s: server length %d != expected %d", url, total, size)
                t0, done = time.time(), have
                with open(part, mode) as f:
                    for blk in r.iter_content(CHUNK):
                        f.write(blk)
                        done += len(blk)
                        if time.time() - t0 > 60:
                            log.info("  %s: %.2f GB%s", os.path.basename(dest), done / 1e9,
                                     f" / {total / 1e9:.2f} GB" if total else "")
                            t0 = time.time()
            if total is not None and os.path.getsize(part) < total:
                raise requests.ConnectionError(f"short read {os.path.getsize(part)} < {total}")
            break
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            wait = min(300, 10 * 2 ** attempt)
            log.warning("download interrupted (%s); resume in %ds", e, wait)
            time.sleep(wait)
    else:
        raise RuntimeError(f"giving up on {url}")
    got = os.path.getsize(part)
    if size is not None and got != size:
        raise RuntimeError(f"size mismatch {dest}: {got} != {size}")
    if md5:
        h = file_md5(part)
        if h != md5:
            os.rename(part, part + ".badmd5")
            raise RuntimeError(f"md5 mismatch {dest}: {h} != {md5}")
        how = "md5 ok"
    else:
        how = _verify_unchecksummed(url, part, dest)
        if how is None:  # local copy was the odd one out: fetch it again (once)
            if _redo:
                raise RuntimeError(f"integrity check failed twice for {dest}")
            os.remove(part)
            return download_file(url, dest, md5, size, test, _redo=True)
    os.replace(part, dest)
    log.info("done: %s (%.3f GB, %s)", dest, got / 1e9, how)
    return "downloaded"


def _stream_md5(url):
    def go():
        h = hashlib.md5()
        with session().get(url, stream=True, timeout=(30, 300), allow_redirects=True) as r:
            r.raise_for_status()
            _check_not_html(r, url)
            for blk in r.iter_content(CHUNK):
                h.update(blk)
        return h.hexdigest()
    return with_retries(go, url)


def _verify_unchecksummed(url, part, dest):
    """No published checksum. Zips: CRC-test every member. Otherwise: stream the file again and compare md5;
    on disagreement a third read decides. Returns a description, or None if the local copy is the bad one."""
    if dest.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(part) as z:
                bad = z.testzip()
        except zipfile.BadZipFile as e:
            log.warning("zip unreadable (%s): %s", e, dest)
            return None
        if bad:
            log.warning("zip CRC error in member %s: %s", bad, dest)
            return None
        return "zip CRC ok"
    local = file_md5(part)
    second = _stream_md5(url)
    if second == local:
        return "verified by 2nd read"
    third = _stream_md5(url)
    log.warning("integrity: %s local %s, re-read %s, third read %s", dest, local, second, third)
    if third == local:
        return "verified by 3rd read"
    return None


def _test_download(url, dest, md5, size):
    """Test mode: small files are downloaded fully (md5-checked); large ones just prove a 1 MiB range works."""
    r = with_retries(lambda: session().get(url, headers={"Range": "bytes=0-1048575"}, stream=True,
                                           timeout=(30, 120), allow_redirects=True), url)
    r.raise_for_status()
    cr = r.headers.get("Content-Range", "")
    total = int(cr.rsplit("/", 1)[1]) if "/" in cr and not cr.endswith("*") else (
        int(r.headers["Content-Length"]) if r.status_code == 200 and "Content-Length" in r.headers else None)
    r.close()
    if size is not None and total is not None and total != size:
        raise RuntimeError(f"server size {total} != manifest size {size}")
    if total is not None and total <= 20 * CHUNK:
        r2 = with_retries(lambda: session().get(url, timeout=(30, 300)), url)
        r2.raise_for_status()
        if md5 and hashlib.md5(r2.content).hexdigest() != md5:
            raise RuntimeError("md5 mismatch")
        with open(dest, "wb") as f:
            f.write(r2.content)
        return f"test: full download ok ({len(r2.content)} B{', md5 ok' if md5 else ''})"
    return f"test: range ok (status {r.status_code}, total {total} B)"


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(16 * CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- directory crawling

_ROW = re.compile(r'<a href="([^"?#][^"]*)">[^<]*</a>\s*</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>\s*([^<]*)</td>', re.S)


def crawl_apache(url, rel=""):
    """Yield (relative_path, file_url) for every file below an Apache autoindex URL."""
    html = with_retries(lambda: _get_text(url), url)
    for href, _, _ in _ROW.findall(html):
        href = unescape(href)
        if href.startswith("/") or href.startswith("..") or "://" in href:
            continue
        name = unquote(href)
        if href.endswith("/"):
            yield from crawl_apache(urljoin(url, href), rel + name)
        else:
            yield rel + name, urljoin(url, href)


def _get_text(url):
    r = session().get(url, timeout=(30, 120))
    r.raise_for_status()
    # An EBI error page with status 200 parsed as an empty folder and dropped 894 EMPIAR-13156 files from a
    # listing (2026-10-15): accept only a real Apache index.
    if "<title>Index of " not in r.text:
        raise requests.ConnectionError(f"not a directory listing: {url}")
    return r.text


def _ftp_names(url):
    """NLST of the FTP twin of an https://ftp.ebi.ac.uk/... directory; None if FTP is unreachable."""
    import ftplib
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        with ftplib.FTP(u.hostname, timeout=60) as ftp:
            ftp.login()
            ftp.cwd(unquote(u.path))
            return [n.rsplit("/", 1)[-1] for n in ftp.nlst()]
    except Exception as e:
        log.info("FTP cross-check unavailable for %s (%s)", url, e.__class__.__name__)
        return None


def _head_exists(url):
    def go():
        r = session().head(url, allow_redirects=True, timeout=(30, 60))
        if r.status_code == 200:
            _check_not_html(r, url)
        return r.status_code == 200
    return with_retries(go, url)


def list_files(base_url, ftp_check=True, probe_gaps=True):
    """Complete file list below an Apache autoindex URL as [(relative_path, url)].

    EBI's HTML listings (and even its FTP listings) silently omit ~0.1-0.3% of entries, so we take the union of
    the HTTPS listing and the FTP listing, and HEAD-probe every gap in numbered file sequences."""
    files = dict(crawl_apache(base_url))
    dirs = sorted({rel.rsplit("/", 1)[0] + "/" if "/" in rel else "" for rel in files})
    added_ftp = added_probe = 0
    for d in dirs:
        durl = urljoin(base_url, "/".join(requests.utils.quote(p) for p in d.split("/")))
        if ftp_check and "ftp.ebi.ac.uk" in base_url:
            names = _ftp_names(durl)
            for n in names or []:
                rel = d + n
                if rel not in files and "." in n:  # skip sub-directories (no extension)
                    files[rel] = urljoin(durl, requests.utils.quote(n))
                    added_ftp += 1
        if probe_gaps:
            groups = {}
            for rel in files:
                if (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "") != d:
                    continue
                name = rel[len(d):]
                m = list(re.finditer(r"\d+", name))
                if not m:
                    continue
                m = m[-1]
                key = (name[: m.start()], name[m.end():], len(m.group()))
                groups.setdefault(key, set()).add(int(m.group()))
            cand = []
            for (pre, suf, width), nums in groups.items():
                if len(nums) < 10:
                    continue
                for n in range(min(nums), max(nums) + 1):
                    if n not in nums:
                        name = f"{pre}{n:0{width}d}{suf}"
                        cand.append((d + name, urljoin(durl, requests.utils.quote(name))))
            if cand:
                with cf.ThreadPoolExecutor(8) as ex:
                    for (rel, u), ok in zip(cand, ex.map(lambda c: _head_exists(c[1]), cand)):
                        if ok:
                            files[rel] = u
                            added_probe += 1
                        else:
                            log.warning("gap in numbered sequence (file really absent): %s", rel)
    if added_ftp or added_probe:
        log.warning("%s: listing incomplete; recovered %d files via FTP and %d via gap probing",
                    base_url, added_ftp, added_probe)
    return sorted(files.items(), key=lambda kv: _natural_key(kv[0]))


# ----------------------------------------------------------------------------- remote chunked arrays

def _numcodec(cfg):
    import numcodecs
    return None if cfg is None else numcodecs.get_codec(dict(cfg))


class ZarrV2Array:
    """Minimal read-only zarr v2 array over HTTP (any compressor numcodecs knows; C or F order)."""

    def __init__(self, url):
        self.url = url.rstrip("/") + "/"
        meta = json.loads(http_get_bytes(self.url + ".zarray"))
        self.shape, self.chunks = tuple(meta["shape"]), tuple(meta["chunks"])
        self.dtype = np.dtype(meta["dtype"])
        self.order = meta.get("order", "C")
        self.sep = meta.get("dimension_separator", ".")
        self.fill = meta.get("fill_value") or 0
        self.compressor = _numcodec(meta.get("compressor"))
        self.filters = [_numcodec(f) for f in (meta.get("filters") or [])]
        # zlib/gzip streams carry a checksum; anything else (raw, zstd, blosc) is verified by double reads
        self.verify = (meta.get("compressor") or {}).get("id") not in ("zlib", "gzip")

    def chunk(self, idx):
        raw = http_get_bytes(self.url + self.sep.join(map(str, idx)), missing_ok=True, verify=self.verify)
        if raw is None:
            return None
        buf = self.compressor.decode(raw) if self.compressor else raw
        for f in reversed(self.filters):
            buf = f.decode(buf)
        return np.frombuffer(buf, dtype=self.dtype).reshape(self.chunks, order=self.order)


class N5Array:
    """Minimal read-only N5 array over HTTP. Exposes axes reversed (N5 xyz -> numpy zyx)."""

    def __init__(self, url):
        self.url = url.rstrip("/") + "/"
        meta = json.loads(http_get_bytes(self.url + "attributes.json"))
        self.shape = tuple(meta["dimensions"][::-1])
        self.chunks = tuple(meta["blockSize"][::-1])
        self.dtype = np.dtype(meta["dataType"]).newbyteorder(">")
        self.comp = meta.get("compression", {"type": "raw"})
        if isinstance(self.comp, str):  # very old N5
            self.comp = {"type": self.comp}
        self.fill = 0
        self.verify = self.comp.get("type") != "gzip"  # gzip has a CRC32

    def chunk(self, idx):
        raw = http_get_bytes(self.url + "/".join(map(str, idx[::-1])), missing_ok=True, verify=self.verify)
        if raw is None:
            return None
        mode, nd = int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")
        bs = [int.from_bytes(raw[4 + 4 * i: 8 + 4 * i], "big") for i in range(nd)]
        off = 4 + 4 * nd + (4 if mode == 1 else 0)
        body = raw[off:]
        t = self.comp["type"]
        if t == "gzip":
            body = zlib.decompress(body, 47)  # gzip or zlib header
        elif t == "raw":
            pass
        else:
            body = _numcodec({"id": {"bzip2": "bz2", "xz": "lzma"}.get(t, t), **{k: v for k, v in self.comp.items() if k != "type"}}).decode(body)
        n = int(np.prod(bs))
        a = np.frombuffer(body, dtype=self.dtype, count=n).reshape(bs[::-1])
        # edge blocks are stored truncated: pad to full chunk shape
        if a.shape != self.chunks:
            full = np.zeros(self.chunks, dtype=self.dtype)
            full[tuple(slice(0, s) for s in a.shape)] = a
            a = full
        return a


def read_chunked(arr, start, stop, workers):
    """Read arr[start:stop] (native axis order) by fetching overlapping chunks in parallel."""
    shape = tuple(b - a for a, b in zip(start, stop))
    out = np.full(shape, arr.fill, dtype=arr.dtype.newbyteorder("="))
    ranges = [range(a // c, (b - 1) // c + 1) for a, b, c in zip(start, stop, arr.chunks)]
    idxs = [()]
    for r in ranges:
        idxs = [i + (j,) for i in idxs for j in r]
    missing = [0]

    def one(idx):
        c = arr.chunk(idx)
        if c is None:
            missing[0] += 1
            return
        sl_src, sl_dst = [], []
        for d, i in enumerate(idx):
            c0 = i * arr.chunks[d]
            a, b = max(start[d], c0), min(stop[d], c0 + arr.chunks[d])
            sl_src.append(slice(a - c0, b - c0))
            sl_dst.append(slice(a - start[d], b - start[d]))
        out[tuple(sl_dst)] = c[tuple(sl_src)]

    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, idxs))
    if missing[0] == len(idxs) and len(idxs) > 0:
        log.warning("all %d chunks missing (filled with %s) for %s", len(idxs), arr.fill, arr.url)
    return out


# ----------------------------------------------------------------------------- crop sources (zyx interface)

class Source:
    """A crop source exposing shape_zyx, dtype and read(z0,z1,y0,y1,x0,x1) -> zyx ndarray."""

    @staticmethod
    def make(spec, workers):
        t = spec["type"]
        if t in ("zarr2", "n5"):
            return ChunkedSource(spec, workers)
        if t == "tiff_slices":
            return TiffSliceSource(spec, workers)
        if t == "zip_slices":
            return ZipSliceSource(spec, workers)
        raise ValueError(f"unknown source type {t}")


class ChunkedSource(Source):
    # "axes" gives the native axis order, e.g. "zyx" (default), "xyz", "cxyz" (channel fixed to 0)
    def __init__(self, spec, workers):
        self.arr = ZarrV2Array(spec["url"]) if spec["type"] == "zarr2" else N5Array(spec["url"])
        self.workers = workers
        axes = spec.get("axes", "zyx")  # numpy axis order (N5Array already reports N5 xyz as zyx)
        self.axes = axes
        if len(axes) != len(self.arr.shape):
            raise ValueError(f"axes {axes} do not match array rank {len(self.arr.shape)}")
        self.shape_zyx = tuple(self.arr.shape[axes.index(a)] for a in "zyx")
        self.dtype = self.arr.dtype.newbyteorder("=")

    def read(self, z0, z1, y0, y1, x0, x1):
        lo, hi = {"z": z0, "y": y0, "x": x0, "c": 0}, {"z": z1, "y": y1, "x": x1, "c": 1}
        a = read_chunked(self.arr, [lo[a] for a in self.axes], [hi[a] for a in self.axes], self.workers)
        if "c" in self.axes:
            a = np.take(a, 0, axis=self.axes.index("c"))
        ax = self.axes.replace("c", "")
        return np.ascontiguousarray(np.transpose(a, [ax.index(a) for a in "zyx"]))


class _SliceListSource(Source):
    """Common logic for 2D-slice-per-z sources. Slice list from an Apache listing + regex, or explicit."""

    def __init__(self, spec, workers):
        self.spec, self.workers = spec, workers

    def read(self, z0, z1, y0, y1, x0, x1):
        out = np.zeros((z1 - z0, y1 - y0, x1 - x0), dtype=self.dtype)

        def one(z):
            out[z - z0] = self.read_slice(z, y0, y1, x0, x1)
        with cf.ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, range(z0, z1)))
        return out


def _natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


class TiffSliceSource(_SliceListSource):
    """One TIFF per z slice (EMPIAR-style), addressed by number: z -> url_template.format(first + z).

    Directory listings on EBI are incomplete, so slices are never enumerated from a listing. A slice that does
    not exist on the server is written as zeros and reported in the sidecar ("missing_slices"), keeping raw and
    label stacks z-aligned. Uncompressed contiguous slices are range-read (only the needed rows)."""

    def __init__(self, spec, workers):
        super().__init__(spec, workers)
        import tifffile  # noqa
        self.template, self.first, self.count = spec["url_template"], spec.get("first", 0), spec["count"]
        self.files = [self.template.format(self.first + z) for z in range(self.count)]
        self.missing = []
        ref = spec.get("ref_slice", self.count // 2)
        p = self._page(self.files[ref])
        self.dtype = np.dtype(p["dtype"])
        self.shape_zyx = (self.count,) + p["shape"]

    def _uncompressed(self, url):
        if not hasattr(self, "_ucomp"):
            self._ucomp = self._page(self.files[self.spec.get("ref_slice", self.count // 2)])["compression"] == 1
        return self._ucomp

    @staticmethod
    def _page(url):
        import tifffile
        with tifffile.TiffFile(io.BufferedReader(HTTPFile(url, verify=True), 1 << 16)) as tf:
            pg = tf.pages[0]
            contig = None
            offs, cnts = pg.dataoffsets, pg.databytecounts
            if (pg.compression == 1 and not pg.is_tiled and pg.predictor == 1 and offs
                    and all(offs[i] + cnts[i] == offs[i + 1] for i in range(len(offs) - 1))):
                contig = (offs[0], sum(cnts))
            return {"shape": tuple(pg.shape[:2]), "dtype": pg.dtype.str, "contig": contig,
                    "ok": pg.samplesperpixel == 1, "compression": int(pg.compression)}

    def read_slice(self, z, y0, y1, x0, x1):
        import tifffile
        url = self.files[z]
        if not _head_exists(url):
            log.warning("slice %d missing on server, writing zeros: %s", z, url)
            self.missing.append(z)
            return np.zeros((y1 - y0, x1 - x0), self.dtype)
        if self.spec.get("range_read", True):
            p = self._page(url)
            if p["contig"] and p["ok"]:
                off, _ = p["contig"]
                h, w = p["shape"]
                isz = np.dtype(p["dtype"]).itemsize
                b = http_get_bytes(url, off + y0 * w * isz, off + y1 * w * isz - 1, verify=True)
                a = np.frombuffer(b, dtype=p["dtype"]).reshape(y1 - y0, w)
                return a[:, x0:x1]
        for attempt in range(4):  # a corrupt transfer shows up as a decode error: re-fetch
            # uncompressed slices have no internal check: require two identical reads
            data = http_get_bytes(url, verify=self._uncompressed(url))
            try:
                a = tifffile.imread(io.BytesIO(data), key=0)
                break
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f"{url}: cannot decode after 4 downloads ({e!r})")
                log.warning("decode failed for %s (%r); re-downloading", url, e)
                time.sleep(5 * (attempt + 1))
        if a.ndim != 2:
            raise RuntimeError(f"{url}: expected 2D slice, got {a.shape}")
        return a[y0:y1, x0:x1]


class ZipSliceSource(_SliceListSource):
    """One TIFF per z slice inside a (remote) zip; individual members are fetched with Range requests."""

    def __init__(self, spec, workers):
        super().__init__(spec, workers)
        self.url = spec["zip_url"]
        self.size = HTTPFile(self.url).size
        zf = zipfile.ZipFile(io.BufferedReader(HTTPFile(self.url, self.size), 1 << 20))
        rx = re.compile(spec.get("regex", r"\.tiff?$"), re.I)
        first, count = spec.get("first", 0), spec["count"]
        # member number = first group of num_regex (default: last digit run of the base name); z = number - first
        nrx = re.compile(spec.get("num_regex", r"(\d+)(?!.*\d)"))
        self.members = {}
        for i in zf.infolist():
            if i.is_dir() or not rx.search(i.filename):
                continue
            m = nrx.search(i.filename.rsplit("/", 1)[-1])
            if not m:
                continue
            num = int(m.group(1))
            if 0 <= num - first < count:
                self.members[num - first] = i
        self.missing = []
        if len(self.members) != count:
            log.warning("%s: %d of %d slices present in zip", self.url, len(self.members), count)
        a = self._member_array(next(iter(self.members)))
        self.dtype = a.dtype
        self.shape_zyx = (count,) + a.shape

    def _member_bytes(self, i):
        info = self.members[i]
        hdr = http_get_bytes(self.url, info.header_offset, info.header_offset + 29)
        if hdr[:4] != b"PK\x03\x04":
            raise RuntimeError("bad local zip header")
        n_name, n_extra = int.from_bytes(hdr[26:28], "little"), int.from_bytes(hdr[28:30], "little")
        start = info.header_offset + 30 + n_name + n_extra
        comp = http_get_bytes(self.url, start, start + info.compress_size - 1) if info.compress_size else b""
        if info.compress_type == zipfile.ZIP_STORED:
            data = comp
        elif info.compress_type == zipfile.ZIP_DEFLATED:
            data = zlib.decompress(comp, -15)
        else:
            raise RuntimeError(f"unsupported zip compression {info.compress_type}")
        if zlib.crc32(data) & 0xFFFFFFFF != info.CRC:
            raise zlib.error(f"CRC mismatch for {info.filename}")
        return data

    def _member_array(self, i):
        import tifffile
        data = with_retries(lambda: self._member_bytes(i), self.members[i].filename)
        return tifffile.imread(io.BytesIO(data), key=0)

    def read_slice(self, z, y0, y1, x0, x1):
        if z not in self.members:
            self.missing.append(z)
            return np.zeros((y1 - y0, x1 - x0), self.dtype)
        return self._member_array(z)[y0:y1, x0:x1]


# ----------------------------------------------------------------------------- OME-Zarr writer

def _ome_attrs(name, voxel_nm_zyx, translation_nm_zyx):
    return {"multiscales": [{
        "version": "0.4", "name": name,
        "axes": [{"name": a, "type": "space", "unit": "nanometer"} for a in "zyx"],
        "datasets": [{"path": "s0", "coordinateTransformations": [
            {"type": "scale", "scale": [float(v) for v in voxel_nm_zyx]},
            {"type": "translation", "translation": [float(v) for v in translation_nm_zyx]}]}],
    }]}


def write_crop(crop, out_dir, workers, test=False, test_size=64, test_slices=8):
    """Read every source of one crop over its box and write <out_dir>/<name>.ome.zarr + <name>.json."""
    import numcodecs
    import zarr

    name = crop["name"]
    zpath = os.path.join(out_dir, name + ".ome.zarr")
    side = os.path.join(out_dir, name + ".json")
    if os.path.exists(side):
        prev_test = json.load(open(side)).get("test_mode", False)
        if not test and not prev_test:
            log.info("crop %s already complete", name)
            return "skipped"
        if test and not prev_test:
            raise RuntimeError(f"{side} is a completed real crop; refusing to overwrite it with a test crop "
                               "(use a different --out for --test)")
        os.remove(side)  # stale sidecar from a previous test run (or test re-run): redo
    os.makedirs(out_dir, exist_ok=True)
    prog_path = os.path.join(zpath, ".progress.json")
    progress = json.load(open(prog_path)) if os.path.exists(prog_path) else {}
    zarr3 = int(zarr.__version__.split(".")[0]) >= 3
    root = zarr.open_group(zpath, mode="a", zarr_format=2) if zarr3 else zarr.open_group(zpath, mode="a")
    sidecar = {"crop": name, "dataset": crop.get("dataset"), "created": dt.datetime.now(dt.timezone.utc).isoformat(),
               "test_mode": test, "arrays": {}}
    comp = numcodecs.Blosc(cname="zstd", clevel=5, shuffle=numcodecs.Blosc.BITSHUFFLE)

    for sspec in crop["sources"]:
        aname = sspec["name"]
        src = Source.make(sspec, workers)
        off = list(sspec.get("offset_zyx", crop["offset_zyx"]))
        size = list(sspec.get("size_zyx", crop["size_zyx"]))
        # clip to the source extent (e.g. thin volumes)
        size = [max(0, min(s, n - o)) for o, s, n in zip(off, size, src.shape_zyx)]
        if any(o < 0 for o in off) or any(s <= 0 for s in size):
            raise RuntimeError(f"{name}/{aname}: box {off}+{size} outside source shape {src.shape_zyx}")
        if test:  # tiny centered box
            slice_like = sspec["type"] in ("tiff_slices", "zip_slices")
            tz = test_slices if slice_like else test_size
            new = []
            for d, (o, s) in enumerate(zip(off, size)):
                t = min(s, tz if d == 0 else test_size)
                new.append((o + (s - t) // 2, t))
            off, size = [o for o, _ in new], [t for _, t in new]
        dtype = np.dtype(sspec.get("out_dtype", src.dtype))
        chunks = tuple(min(c, s) for c, s in zip(sspec.get("chunks", (64, 256, 256)), size))
        key = f"{aname}|{off}|{size}"
        if progress.get(aname, {}).get("key") != key:
            progress[aname] = {"key": key, "done": []}
        arr = None
        if progress[aname]["done"]:
            try:
                arr = root[aname + "/s0"]
            except KeyError:
                progress[aname] = {"key": key, "done": []}
        if arr is None:
            grp = root.require_group(aname)
            if zarr3:
                arr = grp.create_array("s0", shape=tuple(size), chunks=chunks, dtype=dtype,
                                       compressors=comp, fill_value=0, overwrite=True,
                                       chunk_key_encoding={"name": "v2", "separator": "/"})
            else:
                arr = grp.create_dataset("s0", shape=tuple(size), chunks=chunks, dtype=dtype,
                                         compressor=comp, fill_value=0, overwrite=True,
                                         dimension_separator="/")
        vox = sspec.get("voxel_nm_zyx", crop.get("voxel_nm_zyx", [1, 1, 1]))
        # OME translation = where this array sits in the dataset's world grid (source origin + read offset)
        trans_vox = [w + o for w, o in zip(sspec.get("world_offset_vox_zyx", [0, 0, 0]), off)]
        root[aname].attrs.update(_ome_attrs(aname, vox, [t * v for t, v in zip(trans_vox, vox)]))
        root[aname].attrs["source"] = {k: v for k, v in sspec.items() if k != "name"}
        if sspec["type"] == "zarr2" and not test:  # keep the source group's metadata (e.g. label class ids)
            try:
                b = http_get_bytes(sspec["url"].rstrip("/").rsplit("/", 1)[0] + "/.zattrs", missing_ok=True)
                if b and len(b) < 200_000:
                    root[aname].attrs["source_group_attrs"] = json.loads(b)
            except Exception as e:
                log.warning("could not copy source attrs: %s", e)
        root[aname].attrs["offset_vox_zyx"] = off
        slab = chunks[0]
        nslabs = -(-size[0] // slab)
        t0 = time.time()
        for si in range(nslabs):
            if si in progress[aname]["done"]:
                continue
            z0 = si * slab
            z1 = min(size[0], z0 + slab)
            a = src.read(off[0] + z0, off[0] + z1, off[1], off[1] + size[1], off[2], off[2] + size[2])
            if a.dtype != dtype:
                a = a.astype(dtype)
            arr[z0:z1] = a
            progress[aname]["done"].append(si)
            if getattr(src, "missing", None):
                progress[aname]["missing_slices"] = sorted(set(progress[aname].get("missing_slices", [])) |
                                                           set(src.missing))
            with open(prog_path + ".tmp", "w") as f:
                json.dump(progress, f)
            os.replace(prog_path + ".tmp", prog_path)
            log.info("  %s/%s slab %d/%d (z %d-%d) %.0fs", name, aname, si + 1, nslabs, z0, z1, time.time() - t0)
        stats = arr[:] if test else None
        sidecar["arrays"][aname] = {
            "path": f"{os.path.basename(zpath)}/{aname}/s0", "source": {k: v for k, v in sspec.items() if k != "name"},
            "offset_vox_zyx": off, "shape_zyx": size, "source_shape_zyx": list(src.shape_zyx),
            "voxel_nm_zyx": vox, "dtype": dtype.str,
            "missing_source_slices": progress[aname].get("missing_slices", [])}
        if stats is not None:
            sidecar["arrays"][aname]["test_stats"] = {"min": float(stats.min()), "max": float(stats.max()),
                                                      "mean": float(stats.mean()), "nonzero_frac": float((stats != 0).mean())}
    for k in ("note", "labels_note", "voxel_nm_zyx", "offset_zyx", "size_zyx"):
        if k in crop:
            sidecar[k] = crop[k]
    root.attrs["crop"] = {k: v for k, v in sidecar.items() if k != "arrays"}
    with open(side, "w") as f:
        json.dump(sidecar, f, indent=1)
    return sidecar if test else "written"


# ----------------------------------------------------------------------------- dataset handlers

def http_dir_selection(ds, ftp_check=True):
    """[(relative dest path, url)] of an http_dir dataset after its include/exclude filters (also used by verify.py)."""
    inc = [re.compile(r) for r in ds.get("include", [".*"])]
    exc = [re.compile(r) for r in ds.get("exclude", [])]
    todo = []
    for base in ds["urls"]:
        prefix = ds.get("dest_prefix", {}).get(base, "")
        for rel, u in list_files(base, ftp_check=ftp_check):
            relp = prefix + rel
            if any(r.search(relp) for r in inc) and not any(r.search(relp) for r in exc):
                todo.append((relp, u))
    return todo


def run_dataset(ds, out_root, workers, test, args):
    out = os.path.join(out_root, ds["id"])
    os.makedirs(out, exist_ok=True)
    status = {"id": ds["id"], "kind": ds["kind"], "test": test, "started": dt.datetime.now(dt.timezone.utc).isoformat(),
              "items": {}, "failed": []}
    kind = ds["kind"]

    def record(item, fn):
        try:
            status["items"][item] = fn()
        except Exception as e:  # keep going, report at the end
            log.exception("FAILED %s / %s", ds["id"], item)
            status["items"][item] = f"FAILED: {e!r}"
            status["failed"].append(item)

    if kind == "files":
        files = ds["files"]
        with cf.ThreadPoolExecutor(min(workers, ds.get("parallel", 4))) as ex:
            futs = {f["dest"]: ex.submit(_safe, download_file, f["url"], os.path.join(out, f["dest"]),
                                         f.get("md5"), f.get("size"), test) for f in files}
            for k, fu in futs.items():
                res = fu.result()
                status["items"][k] = res
                if str(res).startswith("FAILED"):
                    status["failed"].append(k)
    elif kind == "http_dir":
        todo = http_dir_selection(ds, ftp_check=not test)
        status["n_files"] = len(todo)
        log.info("%s: %d files selected", ds["id"], len(todo))
        if test:  # list + fetch a couple of files' first MiB
            status["items"]["listing"] = f"{len(todo)} files, e.g. {[t[0] for t in todo[:3]]}"
            todo = todo[:2] + todo[-1:]
        with cf.ThreadPoolExecutor(ds.get("parallel", 8)) as ex:
            futs = {rel: ex.submit(_safe, download_file, u, os.path.join(out, rel), None, None, test) for rel, u in todo}
            nd = 0
            for rel, fu in futs.items():
                res = fu.result()
                nd += 1
                if str(res).startswith("FAILED"):
                    status["failed"].append(rel)
                    status["items"][rel] = res
                elif test:
                    status["items"][rel] = res
            if not test:
                status["items"]["files"] = f"{nd - len(status['failed'])}/{nd} ok"
    elif kind == "gdown_folder":
        def go():
            import gdown
            dest = os.path.join(out, ds.get("dest", ""))
            os.makedirs(dest, exist_ok=True)
            import inspect
            want = {"url": ds["url"], "output": dest, "quiet": False, "resume": True,
                    "skip_download": test, "remaining_ok": True}  # kwargs differ across gdown versions
            ok = inspect.signature(gdown.download_folder).parameters
            if test and "skip_download" not in ok:
                return "test: gdown too old for listing-only mode; skipped"
            res = with_retries(lambda: gdown.download_folder(**{k: v for k, v in want.items() if k in ok}), ds["url"])
            if res is None:
                raise RuntimeError("gdown returned None (rate limit / permission?)")
            n = len(res)
            if "expect_files" in ds and n < ds["expect_files"]:
                raise RuntimeError(f"gdown listed {n} files, expected {ds['expect_files']}")
            return f"{'listed' if test else 'downloaded'} {n} files"
        record("folder", go)
    elif kind == "manual":
        with open(os.path.join(out, "MANUAL_DOWNLOAD.txt"), "w") as f:
            f.write(ds["instructions"].strip() + "\n")
        status["items"]["manual"] = "instructions written to MANUAL_DOWNLOAD.txt (not downloaded)"
        status["manual"] = True
    elif kind == "crops":
        for crop in ds["crops"]:
            crop = dict(crop, dataset=ds["id"])
            record(crop["name"], lambda c=crop: write_crop(c, out, workers, test, args.test_size, args.test_slices))
    else:
        raise ValueError(f"unknown kind {kind}")

    # optional extra plain files for any kind (e.g. README)
    for f in ds.get("extra_files", []):
        res = _safe(download_file, f["url"], os.path.join(out, f["dest"]), f.get("md5"), f.get("size"), test)
        status["items"][f["dest"]] = res
        if str(res).startswith("FAILED"):
            status["failed"].append(f["dest"])
    status["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    status["ok"] = not status["failed"]
    with open(os.path.join(out, "_status_test.json" if test else "_status.json"), "w") as f:
        json.dump(status, f, indent=1, default=str)
    return status


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.exception("FAILED %s", a[0] if a else fn)
        return f"FAILED: {e!r}"


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets.json"))
    ap.add_argument("--out", help="output root directory")
    ap.add_argument("--only", nargs="*", help="dataset ids to run")
    ap.add_argument("--task-index", type=int, help="run the N-th dataset (0-based; e.g. $SLURM_ARRAY_TASK_ID)")
    ap.add_argument("--list", action="store_true", help="print dataset ids and exit")
    ap.add_argument("--test", action="store_true", help="smoke test: tiny crops, partial downloads, listings")
    ap.add_argument("--test-size", type=int, default=64, help="test crop edge (chunked sources)")
    ap.add_argument("--test-slices", type=int, default=8, help="test crop depth for slice-based sources")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", 8)) * 2)
    args = ap.parse_args()

    man = json.load(open(args.manifest, encoding="utf-8"))
    dsets = man["datasets"]
    if args.list:
        for i, d in enumerate(dsets):
            print(f"{i:3d}  {d['id']:28s} {d['kind']:12s} {d.get('title', '')}")
        return 0
    if not args.out:
        ap.error("--out is required")
    if args.task_index is not None:
        dsets = [dsets[args.task_index]]
    if args.only:
        known = {d["id"] for d in dsets}
        bad = [o for o in args.only if o not in known]
        if bad:
            ap.error(f"unknown dataset ids: {bad}")
        dsets = [d for d in dsets if d["id"] in args.only]

    os.makedirs(args.out, exist_ok=True)
    if args.test:
        global MAX_RETRIES
        MAX_RETRIES = 2
    tag = "test" if args.test else "run"
    logfile = os.path.join(args.out, f"fetch_{tag}_{'_'.join(d['id'] for d in dsets)[:80]}.log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log.info("manifest %s, %d dataset(s), out=%s, workers=%d, test=%s", args.manifest, len(dsets), args.out,
             args.workers, args.test)
    summary = []
    for d in dsets:
        log.info("=== %s (%s)", d["id"], d["kind"])
        try:
            st = run_dataset(d, args.out, args.workers, args.test, args)
            summary.append((d["id"], st["ok"], st["failed"], st.get("manual", False)))
        except Exception as e:
            log.exception("dataset %s crashed", d["id"])
            summary.append((d["id"], False, [repr(e)], False))
    log.info("===== summary")
    for i, ok, failed, manual in summary:
        log.info("%-28s %s%s", i, "OK" if ok else "FAILED " + str(failed)[:300], " (MANUAL)" if manual else "")
    return 0 if all(ok for _, ok, _, _ in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
