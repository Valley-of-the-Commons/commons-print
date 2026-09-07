#!/usr/bin/env python3
"""
stance.py — gate 1b. How the part lies on the plate, which is the thing gate 1 cannot see.

WHY IT IS A SEPARATE GATE. scad.py's check answers "is this mesh sound" — watertight, faces
agreeing, walls thick enough, fits the bed. A cone standing on its point passes all of it.
So does a 12 cm tower on a 1 cm footprint. Both come off the plate in the first ten minutes
and neither one is a bad mesh; they are a bad *stance*, and that is a different measurement.

WHAT IT MEASURES, all off the triangles, stdlib only:

  CONTACT AREA     the area actually touching z=0, not the bounding box. Triangles whose
                   three corners sit within a twentieth of a millimetre of the lowest point.
  CONTACT RATIO    contact against the part's own shadow. A cone on its base and the same
                   cone on its point have identical bounding boxes and identical volumes;
                   this is the single number that tells them apart.
  TIPPING MARGIN   the centre of mass dropped straight down, against the convex hull of the
                   footprint. How far it is from the nearest edge is how much of a nudge the
                   part survives — from the gantry, from a warping corner, from a door.
  SLENDERNESS      height against the narrow side of the footprint. Tall and thin resonates
                   with the head, and a resonating part is one the toolhead eventually hits.

WHAT IT DOES ABOUT IT, in this order, because the order is the whole point:

    turn it over   ->   brim   ->   raft   ->   refuse

  Most parts that will not stick are not badly shaped, they are badly *placed*: they landed
  on the wrong face because that is how the author left them in the file. Turning the part
  over costs nothing and consumes no filament. A brim costs a gram. A raft costs an hour and
  a scarred bottom face. Reaching for the raft first — which is what a nervous person does,
  and what a nervous piece of software would do — pays the highest price for the easiest
  problem. So `fix` re-orients first and re-measures, and only what is still bad after that
  is allowed to spend filament on it.

The re-orientation is OrcaSlicer's own, run through `--orient 1 --export-stl` so the mesh
that comes back is the mesh that was measured. We do not roll our own: theirs already knows
about overhangs and bed contact together, and a second opinion here would only be a worse
one.

Usage:
  stance.py check <mesh.stl> [--json]         measure it as it lies
  stance.py fix   <mesh.stl> [-o out.stl]     turn it over, measure again, decide
"""
import json, math, subprocess, sys, tempfile
from pathlib import Path

from . import mesh as S
from . import profiles as P

ORCA_BIN = Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")

CONTACT_EPS_MM = 0.05      # a twentieth of a millimetre: inside any first layer
MIN_CONTACT_MM2 = 25.0     # below this there is nothing for the plate to hold, whatever the ratio
GOOD_RATIO = 0.60          # sits on a real face — print as it lies
POOR_RATIO = 0.20          # under this, a brim is not the answer; turning it over might be
TIP_MARGIN_MM = 3.0        # how far the centre of mass must be from the footprint's edge
SLENDER_MAX = 6.0          # height : narrow footprint side


# ---------- measurement ----------

def _xy_area2(a, b, c):
    """Twice the signed area of the triangle projected onto the plate."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])


def hull(points):
    """Monotone chain. The footprint's convex hull is what a part actually tips about — the
    real outline can be a ring or a horseshoe, and neither of those tips inward."""
    pts = sorted(set((round(p[0], 4), round(p[1], 4)) for p in points))
    if len(pts) < 3:
        return pts
    def half(ps):
        out = []
        for p in ps:
            while len(out) >= 2 and _xy_area2(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]
    return half(pts) + half(pts[::-1])


def measure(tris):
    zs = [v[2] for t in tris for v in t]
    xs = [v[0] for t in tris for v in t]
    ys = [v[1] for t in tris for v in t]
    zmin, zmax = min(zs), max(zs)

    # `_xy_area2` returns TWICE the signed area, and a closed mesh projects every point of
    # its shadow twice — once on a face pointing up, once on a face pointing down. So the
    # running total is four times the silhouette, and dividing by anything less quietly
    # halves every ratio: a flat plate reads 50% contact instead of 100%.
    shadow4 = 0.0
    contact = 0.0
    contact_pts = []
    vol6 = 0.0
    cx = cy = cz = 0.0
    for a, b, c in tris:
        s2 = _xy_area2(a, b, c)
        shadow4 += abs(s2)
        if max(a[2], b[2], c[2]) <= zmin + CONTACT_EPS_MM:
            contact += abs(s2) / 2.0
            contact_pts += [a, b, c]
        # Signed tetrahedron against the origin: sums to the volume and, weighted by each
        # tetrahedron's own centroid, to the centre of mass of a solid of even density.
        v6 = (a[0] * (b[1] * c[2] - b[2] * c[1])
              - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
        vol6 += v6
        cx += v6 * (a[0] + b[0] + c[0]) / 4.0
        cy += v6 * (a[1] + b[1] + c[1]) / 4.0
        cz += v6 * (a[2] + b[2] + c[2]) / 4.0

    shadow = shadow4 / 4.0
    com = (cx / vol6, cy / vol6, cz / vol6) if abs(vol6) > 1e-9 else (0, 0, 0)
    h = hull(contact_pts)

    margin = -1.0
    if len(h) >= 3:
        inside = True
        d = float("inf")
        for i in range(len(h)):
            p, q = h[i], h[(i + 1) % len(h)]
            if _xy_area2(p, q, com) < 0:
                inside = False
            ex, ey = q[0] - p[0], q[1] - p[1]
            L2 = ex * ex + ey * ey
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((com[0]-p[0])*ex + (com[1]-p[1])*ey) / L2))
            d = min(d, math.hypot(com[0] - (p[0]+t*ex), com[1] - (p[1]+t*ey)))
        margin = d if inside else -d
    fw = (max(x for x, _ in h) - min(x for x, _ in h)) if h else 0.0
    fd = (max(y for _, y in h) - min(y for _, y in h)) if h else 0.0

    height = zmax - zmin
    narrow = min(fw, fd) if min(fw, fd) > 0 else min(max(xs)-min(xs), max(ys)-min(ys))
    return {
        "contact_mm2": round(contact, 2),
        "shadow_mm2": round(shadow, 2),
        "contact_ratio": round(contact / shadow, 4) if shadow else 0.0,
        "tip_margin_mm": round(margin, 2),
        "footprint_mm": [round(fw, 2), round(fd, 2)],
        "height_mm": round(height, 2),
        "slenderness": round(height / narrow, 2) if narrow else 0.0,
        "com_mm": [round(v, 2) for v in com],
        "volume_mm3": round(abs(vol6) / 6.0, 1),
        "tris": len(tris),
    }


# ---------- the decision ----------

def decide(m):
    """Returns (verdict, why, what slice.py should be told). Nothing here refuses a part for
    being tall; it refuses a part for having nothing to stand on."""
    why, plan = [], {"brim": "auto", "support": "auto"}
    r, c, margin = m["contact_ratio"], m["contact_mm2"], m["tip_margin_mm"]

    if c < MIN_CONTACT_MM2:
        why.append(f"only {c:.1f} mm2 of it touches the plate — there is nothing there for "
                   f"the first layer to hold on to")
        return "turn", why, plan
    if r < POOR_RATIO:
        why.append(f"it touches over {r*100:.0f}% of its own shadow ({c:.0f} of "
                   f"{m['shadow_mm2']:.0f} mm2) — it is resting on an edge or a point, not a face")
        return "turn", why, plan
    if 0 <= margin < TIP_MARGIN_MM:
        why.append(f"its weight lands {margin:.1f} mm from the edge of its footprint — a "
                   f"nudge from the gantry puts it over")
        return "turn", why, plan
    if margin < 0:
        why.append(f"its weight lands outside the footprint entirely — it is already falling")
        return "turn", why, plan

    # The ratio alone is not enough to decide the brim, and a tower is why. A 10 mm square
    # column 120 mm tall touches 100% of its own shadow — perfectly flat, perfectly seated —
    # and still comes off, because 100 mm2 is not much to hold a lever that long. Absolute
    # footprint and leverage get their own say.
    tall = m["slenderness"] > SLENDER_MAX
    close = 0 <= margin < TIP_MARGIN_MM * 2
    if r < GOOD_RATIO:
        plan["brim"] = "on"
        why.append(f"{r*100:.0f}% of its shadow is on the plate — enough to print, not enough "
                   f"to trust. A brim widens what the first layer has to grip.")
    elif tall or close:
        plan["brim"] = "on"
        why.append(f"flat on the plate, but {m['height_mm']:.0f} mm of it stands on "
                   f"{c:.0f} mm2 — a brim is cheap next to starting again")
    else:
        plan["brim"] = "off"
        why.append(f"{r*100:.0f}% of its shadow is flat on the plate — it stands on a real face")

    if tall:
        why.append(f"{m['height_mm']:.0f} mm tall on a {min(m['footprint_mm']):.0f} mm side "
                   f"({m['slenderness']:.1f}:1) — it will ring against the head. Slower, or "
                   f"laid down if the part allows it.")
    return "print", why, plan


# ---------- turning it over ----------

def reorient(src: Path, out: Path):
    """OrcaSlicer's own auto-orientation, made permanent by exporting the mesh back out.
    `--export-stl` lands in a `stl/` folder under the output directory."""
    tmp = Path(tempfile.mkdtemp(prefix="stance-"))
    subprocess.run([str(ORCA_BIN), "--datadir", str(tmp / "dd"), "--debug", "2",
                    "--logfile", str(tmp / "orient.log"),
                    "--orient", "1", "--ensure-on-bed",
                    "--export-stl", "--outputdir", str(tmp), str(src)],
                   capture_output=True, text=True, timeout=600)
    got = sorted(tmp.rglob("*.stl"))
    if not got:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(got[0].read_bytes())
    return out


# ---------- commands ----------

def _load(p):
    p = Path(p).expanduser().resolve()
    if not p.exists():
        sys.exit(f"[stance] no such file: {p}")
    if p.suffix.lower() != ".stl":
        sys.exit(f"[stance] measure an STL. Run it through intake.py first — {p.suffix} may "
                 f"carry more than a shape.")
    tris = S.read_stl(p)
    if not tris:
        sys.exit(f"[stance] no triangles in {p.name}")
    return p, tris


def show(name, m, verdict, why, plan, prefix=""):
    print(f"\n  {prefix}{name}")
    print(f"    touches   {m['contact_mm2']:.0f} mm2 of a {m['shadow_mm2']:.0f} mm2 shadow "
          f"({m['contact_ratio']*100:.0f}%)")
    print(f"    footprint {m['footprint_mm'][0]:.0f} × {m['footprint_mm'][1]:.0f} mm, "
          f"{m['height_mm']:.0f} mm tall  ({m['slenderness']:.1f}:1)")
    print(f"    balance   weight lands {m['tip_margin_mm']:.1f} mm from the nearest edge")
    for w in why:
        print(f"    ·  {w}")
    label = {"print": "✅ STANDS", "turn": "↻ NEEDS TURNING OVER"}[verdict]
    extra = f" — slice with --brim {plan['brim']}" if verdict == "print" else ""
    print(f"\n  {label}{extra}\n")


def cmd_check(argv):
    if not argv:
        sys.exit("usage: stance.py check <mesh.stl> [--json]")
    p, tris = _load(argv[0])
    m = measure(tris)
    verdict, why, plan = decide(m)
    if "--json" in argv:
        print(json.dumps({"file": str(p), "verdict": verdict, "why": why,
                          "plan": plan, **m}, indent=1))
    else:
        show(p.name, m, verdict, why, plan)
    return 0 if verdict == "print" else 1


def cmd_fix(argv):
    if not argv:
        sys.exit("usage: stance.py fix <mesh.stl> [-o out.stl] [--json]")
    p, tris = _load(argv[0])
    before = measure(tris)
    v0, why0, plan0 = decide(before)
    if v0 == "print":
        if "--json" not in argv:
            show(p.name, before, v0, why0, plan0, "as it lies · ")
            print("  Nothing to turn. It already stands on a face.\n")
        else:
            print(json.dumps({"file": str(p), "turned": False, "verdict": v0,
                              "why": why0, "plan": plan0, "before": before}, indent=1))
        return 0

    out = Path(P._flag(argv, "-o") or p.with_name(p.stem + ".turned.stl")).expanduser()
    turned = reorient(p, out)
    if not turned:
        print(f"\n  the slicer could not re-orient {p.name}\n")
        return 1
    after = measure(S.read_stl(turned))
    v1, why1, plan1 = decide(after)

    # A raft is what is left when turning it over did not help. It is the expensive answer and
    # it is only reached here, never first.
    if v1 == "turn":
        plan1 = {"brim": "on", "support": "auto", "raft": True}
        why1.append("turning it over did not find a face either — this one needs a raft, and "
                    "the bottom will carry the scar")
        v1 = "raft"

    if "--json" in argv:
        print(json.dumps({"file": str(p), "turned": str(turned), "verdict": v1, "why": why1,
                          "plan": plan1, "before": before, "after": after}, indent=1))
        return 0
    show(p.name, before, v0, why0, plan0, "as it lies · ")
    show(turned.name, after, "print" if v1 != "turn" else "turn", why1, plan1, "turned over · ")
    gained = after["contact_ratio"] - before["contact_ratio"]
    print(f"  Turning it over moved the contact from {before['contact_ratio']*100:.0f}% to "
          f"{after['contact_ratio']*100:.0f}% of its shadow ({gained*100:+.0f} points).")
    print(f"  Slice this one:  slice.py run {turned} --filament \"...\" "
          f"--brim {plan1['brim']}\n")
    return 0


CMDS = {"check": cmd_check, "fix": cmd_fix}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
