#!/usr/bin/env python3
"""
mesh.py — triangles in, printability out. Gate 1a.

Stdlib only, and deliberately so: this is the one file every other gate leans on, and a
repo that needs numpy before it can read an STL is a repo most people will not run. The
STL parser handles both binary and ASCII, the manifold test is honest about the two
different ways a mesh can be broken, and the island finder catches the part that is
secretly several parts.

Lifted from the CAD lane of a private media engine, where it had already earned its
comments the expensive way. They are kept.
"""
import math, struct
from pathlib import Path


def read_stl(path):
    """Return a list of triangles [(v0,v1,v2), ...]. Handles binary and ASCII."""
    data = path.read_bytes()
    # An ASCII STL starts with 'solid', but so can a binary one — the reliable test is
    # whether the declared triangle count matches the file length.
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + n * 50 == len(data):
            tris = []
            for i in range(n):
                off = 84 + i * 50 + 12          # skip the normal
                vs = struct.unpack("<9f", data[off:off + 36])
                tris.append((vs[0:3], vs[3:6], vs[6:9]))
            return tris
    tris, cur = [], []
    for line in data.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if s.startswith("vertex"):
            cur.append(tuple(float(x) for x in s.split()[1:4]))
            if len(cur) == 3:
                tris.append(tuple(cur))
                cur = []
    return tris



def geometry(tris):
    """bbox, signed volume, area, and a real manifold test."""
    if not tris:
        return None
    xs = [v[0] for t in tris for v in t]
    ys = [v[1] for t in tris for v in t]
    zs = [v[2] for t in tris for v in t]
    bbox = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))

    vol6 = 0.0
    area = 0.0
    edges = {}
    for a, b, c in tris:
        vol6 += (a[0] * (b[1] * c[2] - b[2] * c[1])
                 - a[1] * (b[0] * c[2] - b[2] * c[0])
                 + a[2] * (b[0] * c[1] - b[1] * c[0]))
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area += 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)

        # Quantize to 1 nm before pairing: STL stores float32, so two triangles meeting at
        # the "same" vertex differ in the last bits and a naive compare reports every edge
        # as unpaired. This is the single reason hand-rolled manifold checks usually lie.
        def q(v):
            return (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        for p, r in ((a, b), (b, c), (c, a)):
            edges[(q(p), q(r))] = edges.get((q(p), q(r)), 0) + 1

    # Two different failures, and counting edges as unordered pairs only catches the first.
    # OPEN: an edge with no partner — a hole. FLIPPED: an edge whose partner runs the SAME
    # direction, meaning the two faces disagree about which side is outside. A flipped mesh
    # is watertight by hole-count and still rejected by CGAL and by slicers, which is exactly
    # how a spiral sweep can pass a naive check and then fail every boolean downstream.
    undirected = {}
    for (p, r), n in edges.items():
        undirected[tuple(sorted((p, r)))] = undirected.get(tuple(sorted((p, r))), 0) + n
    open_edges = sum(1 for n in undirected.values() if n != 2)
    flipped = sum(1 for (p, r) in edges if (r, p) not in edges and (p, r) in edges)

    # ISLANDS. A part can be flawless and still unprintable because it is secretly several
    # separate solids — a shelf modelled against a wall it never actually touches, a rail
    # floating a hair off its posts. The slicer prints each one where it sits, and the loose
    # ones fall over on the first layer. Union-find over shared vertices finds them.
    parent = {}
    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for a, b, c in tris:
        qa = (round(a[0], 6), round(a[1], 6), round(a[2], 6))
        qb = (round(b[0], 6), round(b[1], 6), round(b[2], 6))
        qc = (round(c[0], 6), round(c[1], 6), round(c[2], 6))
        union(qa, qb)
        union(qb, qc)
    groups = {}
    for v in list(parent):
        groups.setdefault(find(v), []).append(v)
    islands = sorted((max(max(p[i] for p in vs) - min(p[i] for p in vs) for i in range(3)), len(vs))
                     for vs in groups.values())
    return {
        "tris": len(tris),
        "bbox": bbox,
        "volume_mm3": abs(vol6) / 6.0,
        "area_mm2": area,
        "open_edges": open_edges,
        "flipped": flipped,
        "islands": len(islands),
        "smallest_island": round(islands[0][0], 3) if islands else 0,
        "manifold": open_edges == 0 and flipped == 0,
    }



def printability(g, prof, cfg, printer):
    """Same numbers report() prints, shaped for the UI. One source of truth for both."""
    bed, margin = prof["bed"], cfg["bed_margin_mm"]
    usable = [bed[0] - 2 * margin, bed[1] - 2 * margin, bed[2]]
    w, d, h = g["bbox"]
    fits = w <= usable[0] and d <= usable[1] and h <= usable[2]
    fits_rot = d <= usable[0] and w <= usable[1] and h <= usable[2]
    mean_wall = (2 * g["volume_mm3"] / g["area_mm2"]) if g["area_mm2"] else 0
    vol_cm3 = g["volume_mm3"] / 1000.0
    fl = cfg["filament"]
    shell = min(vol_cm3, g["area_mm2"] * 1.2 / 1000.0)
    grams = (shell + max(0.0, vol_cm3 - shell) * 0.20) * fl["density_g_cm3"]

    problems = []
    if g["open_edges"]:
        problems.append(f"{g['open_edges']} open edges — not watertight")
    if g.get("flipped"):
        problems.append(f"{g['flipped']} flipped faces — winding is inconsistent")
    if not (fits or fits_rot):
        problems.append(f"too big for {bed[0]}x{bed[1]}x{bed[2]}")
    if min(w, d, h) < prof["nozzle"] * 2:
        problems.append(f"thinner than 2 nozzles ({prof['nozzle']*2:.1f} mm)")
    # Only complain when a loose piece is actually too small to stay put. A print plate is
    # several solids on purpose; saying "4 separate pieces" about it is noise, not a finding.
    if g.get("islands", 1) > 1 and g.get("smallest_island", 99) < 5.0:
        problems.append(f"{g['islands']} separate pieces — the smallest is only "
                        f"{g['smallest_island']} mm and will be dragged around the bed")
    if mean_wall < prof["nozzle"] * 2:
        problems.append(f"average wall {mean_wall:.2f} mm is under 2 nozzles "
                        f"({prof['nozzle']*2:.1f} mm)")
    return {
        "mean_wall": round(mean_wall, 2),
        "islands": g.get("islands", 1),
        "smallest_island": g.get("smallest_island", 0),
        "bbox": [round(v, 2) for v in g["bbox"]], "tris": g["tris"],
        "manifold": g["manifold"], "open_edges": g["open_edges"],
        "flipped": g.get("flipped", 0),
        "volume_cm3": round(vol_cm3, 2), "grams": round(grams, 1),
        "usd": round(grams / 1000.0 * fl["usd_per_kg"], 2),
        "material": fl["material"], "printer": printer, "bed": bed,
        "fits": fits, "fits_rotated": fits_rot and not fits,
        # Many pieces is fine — a print plate is several by design. What is NOT fine is a
        # piece too small to stay put: those are the ones that get dragged around the bed.
        "ok": (g["manifold"] and (fits or fits_rot)
               and not (g.get("islands", 1) > 1 and g.get("smallest_island", 99) < 5.0)), "problems": problems,
    }


