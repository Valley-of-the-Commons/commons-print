#!/usr/bin/env python3
"""
profiles.py — what the machine is actually allowed to do, per filament.

THE POINT. Nobody should be inventing nozzle temperatures. OrcaSlicer ships the vendor's own
profile database — 2,198 filament profiles for Bambu alone — and that is the authoritative,
versioned source for every temperature, flow limit, plate temperature and nozzle-hardness
requirement. This module reads that database, resolves its inheritance chains, and turns it
into a yes/no answer about a specific spool on a specific machine.

WHAT IT CATCHES, all from the vendor's own fields rather than folklore:
  * ABRASION      `required_nozzle_HRC` — 3 for normal plastics, 40 for anything carbon
                  filled. Run PA6-CF through a stainless nozzle and you grind it away in
                  a spool. This is the single most expensive mistake available.
  * HOTEND        `nozzle_temperature` against the machine's real ceiling (300 C on X1C).
  * BUILD PLATE   Each material lists a temperature per plate type, and a ZERO means that
                  plate is not usable for it. PLA on the engineering plate reads 0; PETG on
                  the cool plate reads 0. Picking the wrong plate is how PETG welds itself
                  to the sheet.
  * FLOW          `filament_max_volumetric_speed` — TPU is 3.6 mm3/s against PLA's 21. Ask
                  a slicer for PLA speeds with TPU loaded and it will skip and under-extrude.
  * ENCLOSURE     high bed temperature means a warping material that wants a closed chamber.
  * MOISTURE      nylon, PC and TPU are hygroscopic; a wet spool prints visibly worse.

Usage:
  profiles.py printers                  machines this database knows for our vendor
  profiles.py filaments [--type PLA]    what our active machine can run
  profiles.py show   <filament>         every setting that matters, resolved
  profiles.py check  <filament>         the safety verdict for our machine + nozzle
"""
import json, glob, re, sys
from pathlib import Path

from . import config as CFG

PRINTERS = CFG.PRINTERS

# Nozzle hardness in Rockwell C. The X1-Carbon ships hardened steel, which is why it can run
# carbon fibre out of the box — but a swapped-in stainless nozzle cannot, and nothing on the
# printer will stop you.
NOZZLE_HRC = {"hardened steel": 60, "stainless steel": 20, "tungsten carbide": 70, "brass": 15}
DEFAULT_NOZZLE_MATERIAL = "hardened steel"

PLATES = [("cool_plate_temp", "Cool Plate / PLA"), ("eng_plate_temp", "Engineering Plate"),
          ("hot_plate_temp", "High-Temp Plate"), ("textured_plate_temp", "Textured PEI Plate")]

HYGROSCOPIC = ("PA", "PAHT", "PA6", "PA12", "PC", "TPU", "PVA", "PET", "ASA")


def active_printer():
    cfg = json.loads(PRINTERS.read_text())
    name = cfg["active"]
    p = cfg["profiles"][name]
    return name, p


def _load(vendor="BBL"):
    """Every profile keyed by name. Inheritance is resolved on demand, not up front —
    the chains are shallow and this keeps a cold start under a second."""
    root = CFG.orca_profiles()
    if not root:
        # An empty database, not an exception: this is a library call, and the entry points
        # above say the sentence. A caller with no database gets "nothing matches", which is
        # a refusal it already knows how to report.
        return {}
    out = {}
    for kind in ("filament", "machine", "process"):
        for f in glob.glob(str(root / vendor / kind / "**" / "*.json"), recursive=True):
            try:
                d = json.load(open(f))
                if d.get("name"):
                    out[d["name"]] = d
            except Exception:
                pass
    return out


def resolve(db, name, seen=()):
    d = db.get(name)
    if not d:
        return {}
    out = {}
    if d.get("inherits") and d["inherits"] not in seen:
        out.update(resolve(db, d["inherits"], seen + (name,)))
    out.update({k: v for k, v in d.items() if k != "inherits"})
    return out


def val(r, key, default=""):
    v = r.get(key, default)
    if isinstance(v, list):
        return v[0] if v else default
    return v


def num(r, key, default=0.0):
    try:
        return float(val(r, key, default))
    except (TypeError, ValueError):
        return default


def for_machine(db, tag="X1C"):
    """Filaments this machine can load. Bambu names them `... @BBL X1C`; the generic ones
    carry the tag too, so the suffix is a reliable filter and far cheaper than walking
    every profile's compatible_printers list."""
    return sorted(n for n, d in db.items()
                  if d.get("type") == "filament" and d.get("instantiation") == "true"
                  and f"@BBL {tag}" in n)


def audit(r, nozzle_mm, nozzle_material, prof):
    """The verdict. Every rule below reads a vendor field — none of it is folklore."""
    stop, warn, notes = [], [], []
    ftype = val(r, "filament_type", "?")

    hrc_req = num(r, "required_nozzle_HRC")
    hrc_have = NOZZLE_HRC.get(nozzle_material, 0)
    if hrc_req > hrc_have:
        stop.append(f"needs a nozzle of HRC {hrc_req:.0f}+, yours is {nozzle_material} "
                    f"(~HRC {hrc_have}). This is abrasive — it will eat the nozzle.")
    elif hrc_req >= 40:
        notes.append(f"abrasive ({ftype}); your {nozzle_material} nozzle is rated for it")

    t = num(r, "nozzle_temperature")
    ceiling = prof.get("hotend_max_c", 300)
    if t > ceiling:
        stop.append(f"wants {t:.0f} C at the nozzle, the machine tops out at {ceiling} C")

    ok_plates = [(lbl, num(r, k)) for k, lbl in PLATES if num(r, k) > 0]
    if not ok_plates:
        warn.append("no build plate listed for this material — check the spool")

    mvs = num(r, "filament_max_volumetric_speed")
    if mvs and mvs < 6:
        warn.append(f"flow limit {mvs} mm3/s — very slow. Anything tuned for PLA (21) "
                    f"will skip and under-extrude with this loaded.")

    bed = max([v for _, v in ok_plates] or [0])
    if bed >= 90 and not prof.get("enclosed"):
        stop.append(f"needs a {bed:.0f} C bed and this machine has no enclosure — it will warp")
    elif bed >= 90:
        notes.append(f"{bed:.0f} C bed; keep the door and top on for the whole print")

    if any(ftype.upper().startswith(h) for h in HYGROSCOPIC):
        warn.append(f"{ftype} is hygroscopic — dry the spool if it has been open, or you get "
                    f"stringing and weak layers")

    if ftype.upper().startswith("TPU") and "AMS" not in val(r, "name", ""):
        warn.append("soft filament — run it from the external spool holder, not the AMS")

    return stop, warn, notes, ok_plates


# ---------- commands ----------

def cmd_printers(_a):
    db = _load()
    ms = sorted(n for n, d in db.items() if d.get("type") == "machine"
                and d.get("instantiation") == "true")
    name, prof = active_printer()
    print(f"\n  machines in the Bambu profile database ({len(ms)})   ▶ ours: {name}\n")
    for n in ms:
        r = resolve(db, n)
        mark = "▶" if prof.get("model", "") .replace("X1C", "X1 Carbon") in n \
            and str(prof["nozzle"]) in n else " "
        print(f"   {mark} {n:44s} nozzle {val(r,'nozzle_diameter'):5s} "
              f"default {val(r,'default_print_profile','')}")
    print()


def cmd_filaments(argv):
    db = _load()
    want = _flag(argv, "--type")
    names = for_machine(db)
    rows = []
    for n in names:
        r = resolve(db, n)
        ft = val(r, "filament_type", "?")
        if want and want.upper() not in ft.upper():
            continue
        rows.append((ft, n, num(r, "nozzle_temperature"), max(num(r, k) for k, _ in PLATES),
                     num(r, "filament_max_volumetric_speed"), num(r, "required_nozzle_HRC"),
                     num(r, "filament_cost")))
    rows.sort()
    print(f"\n  {len(rows)} filaments the X1-Carbon can run"
          + (f" · type {want.upper()}" if want else "") + "\n")
    print(f"   {'type':9s} {'nozzle':>7s} {'bed':>5s} {'flow':>6s} {'HRC':>4s} {'$/kg':>7s}  profile")
    for ft, n, t, bed, mvs, hrc, cost in rows:
        flag = "🔩" if hrc >= 40 else "  "
        print(f"   {ft:9s} {t:6.0f}C {bed:4.0f}C {mvs:5.1f} {hrc:4.0f} {cost:7.2f} {flag}{n}")
    print("\n   🔩 = abrasive, needs a hardened nozzle\n")


def cmd_show(argv):
    if not argv:
        sys.exit("usage: show <filament profile name>")
    db = _load()
    n = _match(db, argv[0])
    r = resolve(db, n)
    print(f"\n  {n}\n")
    print(f"    material        {val(r,'filament_type')}   vendor {val(r,'filament_vendor','?')}")
    print(f"    nozzle          {num(r,'nozzle_temperature'):.0f} C "
          f"(first layer {num(r,'nozzle_temperature_initial_layer'):.0f} C)")
    print(f"    flow ceiling    {num(r,'filament_max_volumetric_speed')} mm3/s")
    print(f"    density / cost  {num(r,'filament_density')} g/cm3   ${num(r,'filament_cost')}/kg")
    print(f"    nozzle hardness needs HRC {num(r,'required_nozzle_HRC'):.0f}")
    print("    build plates")
    for k, lbl in PLATES:
        v = num(r, k)
        print(f"      {'✅' if v > 0 else '🚫'} {lbl:22s} " + (f"{v:.0f} C" if v > 0 else "not for this material"))
    print()


def cmd_check(argv):
    if not argv:
        sys.exit("usage: check <filament> [--nozzle-material \"stainless steel\"]")
    db = _load()
    n = _match(db, argv[0])
    r = resolve(db, n)
    pname, prof = active_printer()
    nmat = _flag(argv, "--nozzle-material") or DEFAULT_NOZZLE_MATERIAL
    stop, warn, notes, plates = audit(r, prof["nozzle"], nmat, prof)

    print(f"\n  {n}\n  on {pname} · {prof['nozzle']} mm {nmat} nozzle\n")
    for s in stop:
        print(f"    🔴 STOP  {s}")
    for w in warn:
        print(f"    ⚠  {w}")
    for t in notes:
        print(f"    ·  {t}")
    if plates:
        best = max(plates, key=lambda p: p[1])
        print(f"\n    plate    use the {best[0]} at {best[1]:.0f} C")
        others = [f"{l} {v:.0f}C" for l, v in plates if l != best[0]]
        if others:
            print(f"             also fine: {', '.join(others)}")
    print(f"    nozzle   {num(r,'nozzle_temperature'):.0f} C     "
          f"flow ceiling {num(r,'filament_max_volumetric_speed')} mm3/s")
    print(f"\n  {'🔴 DO NOT RUN THIS' if stop else '✅ SAFE TO RUN' + (' — read the warnings' if warn else '')}\n")
    return 1 if stop else 0


def _match(db, q):
    if q in db:
        return q
    hits = [n for n in for_machine(db) if q.lower() in n.lower()]
    if not hits:
        sys.exit(f"[profiles] nothing matches '{q}'. try:  profiles.py filaments")
    if len(hits) > 1 and q not in hits:
        # "Bambu PLA Basic" matches the 0.2 / 0.4 / 0.8 variants. Prefer the one for the
        # nozzle actually fitted rather than making someone type the suffix every time.
        _, prof = active_printer()
        noz = f"{prof['nozzle']} nozzle"
        default = [h for h in hits if noz not in h and "nozzle" not in h.rsplit("@", 1)[-1]] \
            if prof["nozzle"] == 0.4 else [h for h in hits if noz in h]
        if len(default) == 1:
            return default[0]
        exact = [h for h in hits if h.lower().startswith(q.lower())]
        if len(exact) != 1:
            print(f"[profiles] '{q}' matches {len(hits)}:", file=sys.stderr)
            for h in hits[:12]:
                print("   ", h, file=sys.stderr)
            sys.exit(1)
        return exact[0]
    return hits[0]


def _flag(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


CMDS = {"printers": cmd_printers, "filaments": cmd_filaments, "show": cmd_show, "check": cmd_check}

if __name__ == "__main__":
    if not CFG.orca_profiles():
        sys.exit("[profiles] " + CFG.PROFILES_MISSING)
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
