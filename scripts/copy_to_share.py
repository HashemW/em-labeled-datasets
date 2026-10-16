#!/usr/bin/env python3
"""Copy a local folder to a Windows (SMB) share without smbclient or a mount, then check the copy.

For hosts without smbclient (FRCE fsitgl-xfer03p). Uses the pure-Python smbprotocol package
(`pip install smbprotocol`, no admin rights needed) and asks for the password once.

    python copy_to_share.py em_datasets               # copy, then check
    python copy_to_share.py em_datasets --check-only  # only compare local and share (name and size)

Safe to re-run: files already on the share with the same size are skipped, so an interrupted copy continues
where it stopped. Every copied file's size is checked on the share, and the run ends with the same full
comparison as check_share_copy.py. Exit code 0 when everything matches, 1 otherwise.
"""
import argparse
import getpass
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from check_share_copy import local_files, report

try:
    import smbclient
except ImportError:
    sys.exit("ERROR: smbprotocol is not installed. Run: pip install smbprotocol")

CHUNK = 4 << 20
MAX_FAILURES = 20  # stop early (e.g. lost connection) instead of failing every remaining file


def unc(top, rel=""):
    return top + ("\\" + rel.replace("/", "\\") if rel else "")


def remote_tree(top):
    """({relative path with '/': size} of the files, set of folders) below UNC folder top; empty if it is missing."""
    files, dirs = {}, set()
    if not smbclient.path.isdir(top):
        return files, dirs
    stack = [""]
    while stack:
        rel = stack.pop()
        for e in smbclient.scandir(unc(top, rel)):
            r = (rel + "/" if rel else "") + e.name
            if e.is_dir():
                dirs.add(r.lower())
                stack.append(r)
            else:
                files[r] = e.stat().st_size
    return files, dirs


def local_dirs(top):
    out = []
    for d, names, _ in os.walk(top):
        out += [os.path.relpath(os.path.join(d, n), top).replace(os.sep, "/") for n in names]
    return out


def copy_one(src, dst, size):
    for attempt in range(1, 4):
        try:
            with open(src, "rb") as fi, smbclient.open_file(dst, mode="wb") as fo:
                shutil.copyfileobj(fi, fo, CHUNK)
            got = smbclient.stat(dst).st_size
            if got != size:
                raise OSError(f"size on the share is {got}, local {size}")
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(10 * attempt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("local", help="the local folder to copy (e.g. em_datasets); it becomes <remote>/<its name>")
    ap.add_argument("--server", default="fssrgd-cssl01p.ncifcrf.gov")
    ap.add_argument("--share", default="CMM-FS")
    ap.add_argument("--remote", default="Share/2026_SummerStudents/datasets",
                    help="folder on the share to copy into (default: %(default)s)")
    ap.add_argument("--user", default=os.environ.get("USER", ""), help="default: $USER")
    ap.add_argument("--domain", default="NIH")
    ap.add_argument("--port", type=int, default=445)
    ap.add_argument("--workers", type=int, default=4, help="files copied in parallel (default: %(default)s)")
    ap.add_argument("--check-only", action="store_true", help="do not copy, only compare local and share")
    a = ap.parse_args()
    src = os.path.abspath(a.local)
    if not os.path.isdir(src):
        sys.exit(f"ERROR: {a.local} is not a folder")
    password = os.environ.get("SMB_PASSWORD") or getpass.getpass(f"Password for {a.domain}\\{a.user}: ")
    smbclient.register_session(a.server, username=f"{a.domain}\\{a.user}" if a.domain else a.user,
                               password=password, port=a.port)
    top = unc(f"\\\\{a.server}\\{a.share}", a.remote.strip("/\\") + "/" + os.path.basename(src))
    print(f"copy {src}\n  to {top}", flush=True)

    local = local_files(src)
    remote, rdirs = remote_tree(top)
    if not a.check_only:
        rlow = {k.lower(): v for k, v in remote.items()}
        todo = sorted(k for k, v in local.items() if rlow.get(k.lower()) != v)
        total = sum(local[k] for k in todo)
        print(f"{len(local)} local files, {len(local) - len(todo)} already on the share; "
              f"copying {len(todo)} files, {total / 1e9:.2f} GB", flush=True)
        smbclient.makedirs(top, exist_ok=True)
        for d in sorted(local_dirs(src)):  # parents before children
            if d.lower() not in rdirs:
                smbclient.makedirs(unc(top, d), exist_ok=True)

        lock = threading.Lock()
        done = {"n": 0, "bytes": 0, "t": time.time()}
        failed = []
        t0 = time.time()

        def job(rel):
            copy_one(os.path.join(src, rel), unc(top, rel), local[rel])
            with lock:
                done["n"] += 1
                done["bytes"] += local[rel]
                if time.time() - done["t"] > 30 or done["n"] == len(todo):
                    done["t"] = time.time()
                    rate = done["bytes"] / max(done["t"] - t0, 1e-9)
                    eta = (total - done["bytes"]) / rate / 60 if rate else 0
                    print(f"  {time.strftime('%H:%M:%S')}  {done['n']}/{len(todo)} files, "
                          f"{done['bytes'] / 1e9:.2f}/{total / 1e9:.2f} GB, {rate / 1e6:.1f} MB/s, "
                          f"about {eta:.0f} min left", flush=True)

        with ThreadPoolExecutor(max(1, a.workers)) as pool:
            futs = {pool.submit(job, k): k for k in todo}
            for f in as_completed(futs):
                if f.exception() is not None:
                    failed.append((futs[f], f.exception()))
                    print(f"  FAILED {futs[f]}: {f.exception()!r}", flush=True)
                    if len(failed) >= MAX_FAILURES:
                        print(f"{MAX_FAILURES} files failed, stopping. Fix the cause, then run the same command "
                              "again (it skips what is already copied).", flush=True)
                        for g in futs:
                            g.cancel()
                        break
        print(f"copy finished in {(time.time() - t0) / 60:.1f} min, {len(failed)} file(s) failed", flush=True)
        # repeated here because the per-file FAILED lines scroll away behind the long check listing
        errors = {}
        for rel, e in failed:
            errors.setdefault(repr(e), rel)
        for e, rel in list(errors.items())[:5]:
            print(f"  error: {e}\n    first file: {rel}")
        remote, _ = remote_tree(top)

    print("\nchecking: local folder vs share (name and size)")
    return report(local, remote, "run the same copy_to_share.py command again (it only sends what is missing or "
                                 "different), then it checks again")


if __name__ == "__main__":
    sys.exit(main())
