#!/usr/bin/env python3
"""Check that a folder copied to a Windows (SMB) share with smbclient arrived complete.

Compares every file of the local folder with a recursive smbclient listing of the copy (name and size) and
lists files that are missing on the share, have a different size, or exist only on the share.

    smbclient //server/SHARE -W NIH -U <user> -c 'cd <remote folder>; recurse on; ls' > share_listing.txt
    python check_share_copy.py em_datasets share_listing.txt --remote-root '<remote folder>'

Exit code 0 when everything matches, 1 otherwise. Standard library only (no venv needed).
"""
import argparse
import os
import re
import sys

# "  name   A   1234  Thu Oct 15 18:00:00 2026" (the day of month is space-padded, e.g. "Oct  5")
ENTRY = re.compile(r"^  (.+?)\s+([A-Z]*)\s+(\d+)\s+\w{3} \w{3}\s+\d{1,2} \d\d:\d\d:\d\d \d{4}\s*$")


def parse_listing(lines, remote_root):
    """{relative path with '/': size} of the files in a recursive smbclient 'ls'."""
    root = "\\" + remote_root.replace("/", "\\").strip("\\")
    files, cur = {}, ""
    for line in lines:
        line = line.rstrip("\r\n")
        if line.startswith("\\"):  # header of a sub-folder listing, e.g. \Share\...\em_datasets\asem-crops
            d = line.rstrip("*").rstrip("\\")
            if d.lower() == root.lower():
                cur = ""
            elif d.lower().startswith(root.lower() + "\\"):
                cur = d[len(root) + 1:].replace("\\", "/")
            else:
                sys.exit(f"ERROR: listing folder {d!r} is not below --remote-root {remote_root!r}")
            continue
        m = ENTRY.match(line)
        if not m or "D" in m.group(2):
            continue  # folders, ".", "..", the "blocks available" line, blank lines
        files[(cur + "/" if cur else "") + m.group(1)] = int(m.group(3))
    return files


def local_files(top):
    out = {}
    for d, _, names in os.walk(top):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out[os.path.relpath(p, top).replace(os.sep, "/")] = os.path.getsize(p)
    return out


def report(local, remote, again):
    """Print what differs between {path: size} dicts local and remote; 0 when all local files match, else 1."""
    gb = lambda d: sum(d.values()) / 1e9  # noqa: E731
    print(f"local: {len(local)} files, {gb(local):.3f} GB")
    print(f"share: {len(remote)} files, {gb(remote):.3f} GB")
    # Windows file names are case-insensitive
    rlow = {k.lower(): (k, v) for k, v in remote.items()}
    missing = [k for k in local if k.lower() not in rlow]
    wrong = [(k, local[k], rlow[k.lower()][1]) for k in local
             if k.lower() in rlow and rlow[k.lower()][1] != local[k]]
    llow = {k.lower() for k in local}
    extra = [k for k in remote if k.lower() not in llow]
    for title, rows in (("missing on the share", missing), ("different size", wrong), ("only on the share", extra)):
        if rows:
            print(f"\n{len(rows)} file(s) {title}:")
            for r in sorted(rows)[:10]:
                print("  " + (r if isinstance(r, str) else f"{r[0]}  local {r[1]} bytes, share {r[2]} bytes"))
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
    if not missing and not wrong:
        print("\nOK: every local file is on the share with the same size")
        return 0
    print(f"\nNOT COMPLETE: {again}")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("local", help="the local folder that was copied (e.g. em_datasets)")
    ap.add_argument("listing", help="file with the output of: smbclient ... -c 'cd <remote>; recurse on; ls'")
    ap.add_argument("--remote-root", default="Share/2026_SummerStudents/datasets/em_datasets",
                    help="the folder on the share that the listing was made in (default: %(default)s)")
    a = ap.parse_args()
    if not os.path.isdir(a.local):
        sys.exit(f"ERROR: {a.local} is not a folder")
    with open(a.listing, encoding="utf-8", errors="replace") as f:
        remote = parse_listing(f, a.remote_root)
    return report(local_files(a.local), remote,
                  "run the same mput command again (it re-sends everything), then check again")


if __name__ == "__main__":
    sys.exit(main())
