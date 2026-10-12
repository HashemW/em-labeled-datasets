"""Measure how similar neighbouring z-slices are, compared with neighbouring xy pixels, on real raw data.

For each labeled crop window: read N slices x S x S from the middle of the window, then
  corr_z(k)  = mean Pearson r between slice z and slice z+k
  corr_xy(d) = mean Pearson r between the image and itself shifted by d pixels in x / y
The z-stride at which slices are "as different as" the in-plane neighbours at the same physical
distance tells us whether z is oversampled. Near-duplicate slices (r > 0.98) are also flagged."""
import json, sys
import numpy as np
sys.path.insert(0, sys.argv[1])
import fetch_em as F

N, S = int(sys.argv[3]), 512
man = {d["id"]: d for d in json.load(open(sys.argv[2]))["datasets"]}
want = sys.argv[5:] or ["openorganelle-crops", "asem-crops", "empiar-10791-crops", "empiar-13420-crops", "fastem-crops"]


def r(a, b):
    a = a.astype(np.float64).ravel(); b = b.astype(np.float64).ravel()
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d else float("nan")


out = []
for did in want:
    for c in man[did]["crops"]:
        src = next(s for s in c["sources"] if s["name"] == "raw")
        vz, vy, vx = c["voxel_nm_zyx"]
        try:
            s = F.Source.make(src, 16)
            Z, Y, X = s.shape_zyx
            oz, oy, ox = c["offset_zyx"]
            ez, ey, ex = (min(o + n, m) for o, n, m in zip(c["offset_zyx"], c["size_zyx"], (Z, Y, X)))
            n = min(N, ez - oz)
            z0 = (oz + ez) // 2 - n // 2
            y0 = (oy + ey) // 2 - S // 2; x0 = (ox + ex) // 2 - S // 2
            a = s.read(z0, z0 + n, y0, y0 + S, x0, x0 + S)
        except Exception as e:
            print(did, c["name"], "FAILED", repr(e)[:200], flush=True); continue
        kmax = min(16, n - 1)
        cz = [np.mean([r(a[i], a[i + k]) for i in range(n - k)]) for k in range(1, kmax + 1)]
        cxy = [np.mean([r(a[i, :, :-d], a[i, :, d:]) for i in range(0, n, 4)] +
                       [r(a[i, :-d, :], a[i, d:, :]) for i in range(0, n, 4)]) for d in range(1, 9)]
        adj = [r(a[i], a[i + 1]) for i in range(n - 1)]
        dup = sum(x > 0.98 for x in adj)
        # physical distance at which in-plane correlation equals corr_z(1)
        res = dict(dataset=did, crop=c["name"], voxel_nm_zyx=[vz, vy, vx], slices=n,
                   corr_z=[round(x, 3) for x in cz], corr_xy=[round(x, 3) for x in cxy],
                   near_duplicate_adjacent=dup, adj_min=round(min(adj), 3), adj_max=round(max(adj), 3))
        out.append(res)
        print(json.dumps(res), flush=True)
json.dump(out, open(sys.argv[4], "w"), indent=1)
