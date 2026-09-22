#!/usr/bin/env python3
"""
slice.py — gate 2 and gate 3 of the workshop. The real slicer, headless, and the truth
that comes out the other side.

WHY THIS EXISTS. Everything upstream of here is an estimate. scad.py knows the volume of
the mesh; it does not know that the part needs a brim, that supports will double the
filament, or that this spool's flow ceiling turns a 1-hour job into 4. Only the slicer
knows, and the slicer that ships the vendor's own profiles is the only one worth asking.
So we run OrcaSlicer's CLI against the real machine profile and the real spool, and we
read the answer out of the G-code rather than guessing at it.

Two gates in one pass:
  GATE 2  the slice itself. Refuses before it starts if profiles.py says the filament is
          wrong for this machine, and refuses after if the slicer itself complained.
  GATE 3  the truth: minutes, grams, layers and height, parsed from the G-code the machine
          would actually run. This is the number the shelf check and the queue quote from.

FOUR THINGS THAT COST AN AFTERNOON, written down so they only cost it once:

  1. `--export-3mf` must be a BARE FILENAME. The CLI joins it onto `--outputdir` without
     checking, so an absolute path silently becomes `<outputdir><abspath>` and the run
     ends in `Unable to open the file` after slicing successfully.

  2. Presets must be FULLY FLATTENED, with `"from": "system"`. Three shapes fail:
       from "User", flattened      -> "process not compatible with printer", always,
                                      regardless of compatible_printers or settings ids.
       from "User", thin+inherits  -> passes the compatibility check and then resolves the
                                      inheritance WRONG. Same mesh, same presets: 4h19m and
                                      filament_density 0, against 1h13m and 1.26 for the
                                      flattened pair. It slices, and it lies.
       from "system", flattened    -> correct.
     So we resolve the inheritance ourselves (profiles.resolve) and hand the CLI finished
     configs. It never gets a chance to resolve anything.

  3. `curr_bed_type` defaults to **Cool Plate** no matter what machine you loaded. An X1C
     has textured PEI on it. Left alone, PLA gets a 35 C bed instead of 65 and the vendor's
     own plate-compatibility table — the thing profiles.py exists to enforce — is bypassed.
     We always set it explicitly.

  4. There is no OpenGL in a headless run, so thumbnail generation fails and says so. It is
     harmless: the plate renders fine when Bambu Studio opens the file.

Usage:
  slice.py filaments [--type PLA]              what this machine can run
  slice.py layers                              process profiles for the fitted nozzle
  slice.py run <mesh> --filament <name> [opts] slice it and report the truth
  slice.py matrix <mesh> --filaments "a,b" [--layers "0.12,0.20"]
                                               the smoke test: every row is a real slice
  slice.py show <job-dir>                      re-read a finished job's numbers

Options for `run`:
  --printer <key>       a profile in scad/printers.json      (default: the active one)
  --process <name>      an Orca process preset               (default: the machine's own)
  --layer 0.20          pick the process by layer height instead of naming it
  --quality <name>      Standard | Strength | Fine ... , with --layer   (default: Standard)
  --plate <name>        Cool Plate | Engineering Plate | High Temp Plate | Textured PEI Plate
                        (default: the best one the filament actually allows)
  --nozzle-material     stainless steel | hardened steel     (default: hardened steel)
  --brim on|off|auto    (default: auto — the process decides)
  --support on|off|auto (default: auto)
  --orient auto|off     (default: off. Auto-orientation is gate 1b's decision, not ours;
                        it changes how the part lies on the plate and must be reported.)
  --set k=v             any process override, repeatable. The escape hatch, used sparingly.
  --out <dir>           where the job lands   (default: runs/jobs/<stamp>)
  --json                machine-readable verdict on stdout, nothing else
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

from . import config as CFG

from . import profiles as P

JOBS = CFG.RUNS / "jobs"
SLICE_TIMEOUT_S = 900

# profiles.py's plate labels are ours; these are the strings OrcaSlicer's enum will accept.
PLATE_ENUM = {"Cool Plate / PLA": "Cool Plate", "Engineering Plate": "Engineering Plate",
              "High-Temp Plate": "High Temp Plate", "Textured PEI Plate": "Textured PEI Plate"}
# What is physically on the machine. Preferred whenever the filament allows it.
PLATE_FITTED = "Textured PEI Plate"

MODEL_FULL = {"X1C": "Bambu Lab X1 Carbon", "X1": "Bambu Lab X1", "X1E": "Bambu Lab X1E",
              "P1S": "Bambu Lab P1S", "P1P": "Bambu Lab P1P", "A1": "Bambu Lab A1",
              "A1M": "Bambu Lab A1 mini", "H2D": "Bambu Lab H2D"}


# ---------- preset assembly ----------

def machine_preset(db, prof):
    """The Orca machine preset for a printers.json profile, found by its own fields rather
    than by guessing at the name. printer_model + printer_variant is the identifying pair."""
    want_model = MODEL_FULL.get(prof.get("model", ""), prof.get("model", ""))
    want_noz = str(prof.get("nozzle", 0.4))
    hits = []
    for n, d in db.items():
        if d.get("type") != "machine" or d.get("instantiation") != "true":
            continue
        r = P.resolve(db, n)
        if P.val(r, "printer_model") == want_model and P.val(r, "printer_variant") == want_noz:
            hits.append(n)
    if not hits:
        sys.exit(f"[slice] no Orca machine preset for {want_model} @ {want_noz} mm")
    return sorted(hits, key=len)[0]


def process_profiles(db, prof):
    """Layer-height profiles that match the nozzle actually fitted. Orca names the 0.4 mm
    ones with no nozzle suffix and tags every other nozzle explicitly, so the filter has to
    work both ways or you silently cut a 0.2 mm job with 0.4 mm settings."""
    tag = prof.get("model", "X1C")
    out = {}
    for n, d in db.items():
        if d.get("type") != "process" or f"@BBL {tag}" not in n:
            continue
        tagged = re.search(r"(\d\.\d) nozzle$", n)
        noz = float(prof.get("nozzle", 0.4))
        if (tagged and float(tagged.group(1)) != noz) or (not tagged and noz != 0.4):
            continue
        m = re.match(r"(\d\.\d\d)mm (.+?) @", n)
        if m:
            out.setdefault(m.group(1), []).append((m.group(2), n))
    return out


def pick_process(db, prof, layer, quality="Standard"):
    procs = process_profiles(db, prof)
    key = f"{float(layer):.2f}"
    if key not in procs:
        sys.exit(f"[slice] no {key} mm profile for this nozzle. have: " + ", ".join(sorted(procs)))
    for q, n in procs[key]:
        if q == quality:
            return n
    return procs[key][0][1]


def cmd_layers(_a):
    printers = json.loads(CFG.PRINTERS.read_text())
    prof = printers["profiles"][printers["active"]]
    procs = process_profiles(P._load(), prof)
    print(f"\n  process profiles for the {prof.get('nozzle')} mm nozzle on "
          f"{printers['active']}\n")
    for k in sorted(procs):
        print(f"    {k} mm   " + ", ".join(sorted(q for q, _ in procs[k])))
    print()
    return 0


def flatten(db, name, kind, overrides=None):
    """A finished config the CLI cannot misread. See note 2 in the docstring."""
    r = P.resolve(db, name)
    if not r:
        sys.exit(f"[slice] no such {kind} preset: {name}")
    r["name"] = name
    r["type"] = kind
    r["from"] = "system"          # anything else is refused as incompatible
    r.pop("inherits", None)
    r.pop("instantiation", None)
    for k, v in (overrides or {}).items():
        r[k] = v
    return r


# ---------- gate 3: reading the G-code ----------

# Full regexes, not fragments: `total estimated time` shares a line with `model printing
# time`, so anchoring every one of these to the start of a comment silently returns zero.
TRUTH = [
    ("minutes",     r"total estimated time:\s*([^;\n]+)",           "time"),
    ("print_min",   r"^;\s*model printing time:\s*([^;\n]+)",      "time"),
    ("layers",      r"^;\s*total layer number:\s*(\d+)",           "int"),
    ("max_z_mm",    r"^;\s*max_z_height:\s*([\d.]+)",              "float"),
    ("grams",       r"^;\s*filament used \[g\]\s*=\s*([\d.,\s]+)",  "sum"),
    ("cm3",         r"^;\s*filament used \[cm3\]\s*=\s*([\d.,\s]+)", "sum"),
    ("filament_mm", r"^;\s*filament used \[mm\]\s*=\s*([\d.,\s]+)",  "sum"),
    ("bed_type",    r"^;\s*curr_bed_type\s*=\s*(.+)$",             "str"),
    ("nozzle_mm",   r"^;\s*nozzle_diameter\s*=\s*([\d.]+)",       "float"),
    ("nozzle_c",    r"^;\s*nozzle_temperature\s*=\s*(\d+)",         "int"),
    ("bed_c",       r"^;\s*first_layer_bed_temperature\s*=\s*(\d+)", "int"),
    ("supported",   r"^;\s*enable_support\s*=\s*(\d)",             "int"),
]


def _mins(s):
    """`4h 19m 41s` / `1h 6m 12s` / `48m 3s` -> minutes, rounded up. The slicer writes it
    for humans, and the queue needs to add it up."""
    h = re.search(r"(\d+)\s*h", s)
    m = re.search(r"(\d+)\s*m", s)
    sec = re.search(r"(\d+)\s*s", s)
    total = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    return total + (1 if sec and int(sec.group(1)) else 0)


def read_truth(gcode: Path):
    """Bambu writes the header stats at the top and the material totals at the bottom, so
    both ends are read and the middle — several megabytes of moves — is never touched."""
    size = gcode.stat().st_size
    WINDOW = 262144
    with gcode.open("rb") as fh:
        if size <= WINDOW * 2:
            # Anything that fits in both windows is read whole. Reading a head window and
            # only adding a tail past 2×WINDOW leaves a gap — a file of 300 KB gets its first
            # 256 KB read and its footer, where the material totals live, never seen.
            text = fh.read().decode("utf8", "replace")
        else:
            head = fh.read(WINDOW).decode("utf8", "replace")
            fh.seek(-WINDOW, os.SEEK_END)
            text = head + "\n" + fh.read().decode("utf8", "replace")
    out = {}
    for key, pat, kind in TRUTH:
        m = re.search(pat, text, re.M)
        if not m:
            continue
        raw = m.group(1).strip()
        if kind == "time":
            out[key] = _mins(raw)
        elif kind == "int":
            out[key] = int(raw)
        elif kind == "float":
            out[key] = float(raw)
        elif kind == "sum":
            out[key] = round(sum(float(x) for x in raw.split(",") if x.strip()), 2)
        else:
            out[key] = raw
    return out


# ---------- the run ----------

def cmd_run(argv):
    if not argv or argv[0].startswith("-"):
        sys.exit("usage: slice.py run <mesh.stl|.3mf|.step> --filament <name> [...]")
    mesh = Path(argv[0]).expanduser().resolve()
    if not mesh.exists():
        sys.exit(f"[slice] no such file: {mesh}")

    as_json = "--json" in argv
    fila_q = P._flag(argv, "--filament")
    if not fila_q:
        sys.exit("[slice] --filament is required. `slice.py filaments` lists them.")

    printers = json.loads(CFG.PRINTERS.read_text())
    pkey = P._flag(argv, "--printer") or printers["active"]
    prof = printers["profiles"].get(pkey)
    if not prof:
        sys.exit(f"[slice] no printer profile '{pkey}' in {CFG.PRINTERS}")

    db = P._load()
    mname = machine_preset(db, prof)
    mres = P.resolve(db, mname)
    fname = P._match(db, fila_q)
    fres = P.resolve(db, fname)
    layer = P._flag(argv, "--layer")
    pname = (P._flag(argv, "--process")
             or (pick_process(db, prof, layer, P._flag(argv, "--quality") or "Standard")
                 if layer else P.val(mres, "default_print_profile")))
    nmat = P._flag(argv, "--nozzle-material") or P.DEFAULT_NOZZLE_MATERIAL

    # ---- gate 2a: refuse before spending the minutes ----
    stop, warn, notes, ok_plates = P.audit(fres, prof.get("nozzle", 0.4), nmat, prof)

    plate = P._flag(argv, "--plate")
    allowed = {PLATE_ENUM[l]: v for l, v in ok_plates}
    if plate:
        if plate not in allowed:
            stop.append(f"the {plate} is not usable with {P.val(fres,'filament_type')} — "
                        f"the vendor lists it at 0 C. Allowed: {', '.join(allowed) or 'none'}")
    elif allowed:
        plate = PLATE_FITTED if PLATE_FITTED in allowed else max(allowed, key=allowed.get)
        if plate != PLATE_FITTED:
            notes.append(f"needs the {plate} on the machine — the {PLATE_FITTED} is the one "
                         f"normally fitted, and this material cannot use it")

    verdict = {"ok": False, "mesh": str(mesh), "printer": pkey, "machine": mname,
               "process": pname, "filament": fname,
               "material": P.val(fres, "filament_type"), "plate": plate,
               "nozzle_material": nmat, "refusals": stop, "warnings": warn, "notes": notes}

    if stop:
        return _finish(verdict, as_json, None)

    # ---- assemble ----
    outdir = Path(P._flag(argv, "--out") or (JOBS / time.strftime("%Y%m%d-%H%M%S"))).expanduser()
    work = outdir / "cfg"
    work.mkdir(parents=True, exist_ok=True)
    (outdir / "out").mkdir(exist_ok=True)

    proc_over = {"curr_bed_type": plate}      # note 3: never leave this to the default
    brim = (P._flag(argv, "--brim") or "auto").lower()
    if brim == "on":
        proc_over.update({"brim_type": "outer_only", "brim_width": "5"})
    elif brim == "off":
        proc_over["brim_type"] = "no_brim"
    sup = (P._flag(argv, "--support") or "auto").lower()
    if sup in ("on", "off"):
        proc_over["enable_support"] = "1" if sup == "on" else "0"
    for i, a in enumerate(argv):
        if a == "--set" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, v = argv[i + 1].split("=", 1)
            proc_over[k] = v

    json.dump(flatten(db, mname, "machine"), (work / "machine.json").open("w"), indent=1)
    json.dump(flatten(db, pname, "process", proc_over), (work / "process.json").open("w"), indent=1)
    json.dump(flatten(db, fname, "filament"), (work / "filament.json").open("w"), indent=1)

    orient = "1" if (P._flag(argv, "--orient") or "off").lower() == "auto" else "0"
    log = outdir / "slicer.log"
    orca = CFG.orca_command()
    if not orca:
        # Reached through workshop.py as well as its own __main__, so the guard lives here
        # too: without it a bench with no slicer gets `None + list`, not this sentence.
        sys.exit("[slice] " + CFG.ORCA_MISSING)
    cmd = orca + [
        "--datadir", str(work / "datadir"), "--debug", "4",
        "--logfile", str(log),
        "--load-settings", f"{work/'machine.json'};{work/'process.json'}",
        "--load-filaments", str(work / "filament.json"),
        "--orient", orient, "--arrange", "1", "--ensure-on-bed",
        "--slice", "0",
        "--export-3mf", "job.3mf",          # note 1: bare filename, never a path
        "--outputdir", str(outdir / "out"),
        str(mesh)]
    (outdir / "command.txt").write_text(" \\\n  ".join(cmd) + "\n")

    t0 = time.time()
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=SLICE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        verdict["refusals"].append(f"the slicer ran past {SLICE_TIMEOUT_S//60} minutes and was "
                                   f"stopped — the mesh is too heavy to gate automatically")
        return _finish(verdict, as_json, outdir)
    verdict["slicer_seconds"] = round(time.time() - t0, 1)

    logtext = log.read_text(errors="replace") if log.exists() else ""
    verdict["slicer_complaints"] = complaints(logtext)

    job = outdir / "out" / "job.3mf"
    gcodes = sorted((outdir / "out").glob("*.gcode"))
    if not job.exists() or not gcodes:
        verdict["refusals"].append(
            complaints(logtext, first=True) or "the slicer produced nothing and said nothing "
            "useful — see slicer.log")
        return _finish(verdict, as_json, outdir)

    verdict["job"] = {"3mf": str(job), "gcode": str(gcodes[0]), "plates": len(gcodes)}
    verdict["truth"] = read_truth(gcodes[0])
    t = verdict["truth"]
    cost = P.num(fres, "filament_cost")
    if cost and t.get("grams"):
        t["usd"] = round(t["grams"] / 1000 * cost, 2)
    if t.get("bed_type") and t["bed_type"] != plate:
        verdict["warnings"].append(f"the G-code says {t['bed_type']} but we asked for {plate}")
    verdict["ok"] = True
    return _finish(verdict, as_json, outdir)


# Lines the slicer itself objects to. Its refusals are part of the gate — it already knows
# about objects off the bed, empty layers and plates that cannot hold a temperature, and
# reimplementing any of that would only produce a second, worse opinion.
# Chatter the slicer emits on every healthy run. `removing top empty layers` is the
# normal end of a solid object, not a complaint about one.
NOISE = re.compile(
    r"Invalid OpenGL|glfwInit|thumbnail|GLFW|opengl|removing top empty layers|"
    r"m_shared_object|slice result from", re.I)
COMPLAINT = re.compile(r"(error|invalid|failed|cannot|unable|not compatible|exceed|out of|"
                       r"empty layer|too (small|large|many)|no extrusions)", re.I)


def complaints(logtext, first=False):
    out = []
    for line in logtext.splitlines():
        line = line.strip()
        if not line or NOISE.search(line) or not COMPLAINT.search(line):
            continue
        if line not in out:
            out.append(line)
    return (out[0] if out else "") if first else out


def _finish(v, as_json, outdir):
    if outdir:
        (outdir / "verdict.json").write_text(json.dumps(v, indent=1) + "\n")
    if as_json:
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] else 1
    report(v)
    return 0 if v["ok"] else 1


def report(v):
    print(f"\n  {Path(v['mesh']).name}")
    print(f"  {v['machine']} · {v['filament']} · {v['plate'] or 'no plate'}\n")
    for s in v["refusals"]:
        print(f"    🔴 STOP  {s}")
    for w in v["warnings"]:
        print(f"    ⚠  {w}")
    for n in v["notes"]:
        print(f"    ·  {n}")
    for c in v.get("slicer_complaints", [])[:6]:
        print(f"    ⚠  slicer: {c}")
    t = v.get("truth") or {}
    if t:
        hrs = t.get("minutes", 0)
        print(f"\n    time     {hrs//60}h {hrs%60:02d}m"
              + (f"   (moving: {t['print_min']//60}h {t['print_min']%60:02d}m)" if "print_min" in t else ""))
        print(f"    filament {t.get('grams','?')} g   ({t.get('cm3','?')} cm3)"
              + (f"   ${t['usd']}" if "usd" in t else ""))
        print(f"    heat     nozzle {t.get('nozzle_c','?')} C · first-layer bed "
              f"{t.get('bed_c','?')} C" + ("   supports on" if t.get("supported") else ""))
        print(f"    build    {t.get('layers','?')} layers, {t.get('max_z_mm','?')} mm tall")
        print(f"    plate    {t.get('bed_type','?')}   nozzle {t.get('nozzle_mm','?')} mm")
        print(f"\n    3mf      {v['job']['3mf']}")
    print(f"\n  {'✅ READY — this is the file to print' if v['ok'] else '🔴 NOT PRINTED'}\n")


def cmd_show(argv):
    if not argv:
        sys.exit("usage: slice.py show <job-dir>")
    d = Path(argv[0]).expanduser()
    f = d / "verdict.json" if d.is_dir() else d
    report(json.loads(f.read_text()))
    return 0


def cmd_matrix(argv):
    """The smoke test. `which settings` stops being a matter of opinion when every row on
    the table is a slice that actually ran. Each combination goes through the same gates as
    a real job — a filament this machine cannot take is refused here too, and says why."""
    if not argv or argv[0].startswith("-"):
        sys.exit('usage: slice.py matrix <mesh> --filaments "PLA Basic,PETG HF" '
                 '[--layers "0.12,0.20,0.28"]')
    mesh = Path(argv[0]).expanduser().resolve()
    fils = [x.strip() for x in (P._flag(argv, "--filaments") or "Bambu PLA Basic").split(",")]
    layers = [x.strip() for x in (P._flag(argv, "--layers") or "0.12,0.20,0.28").split(",")]
    root = Path(P._flag(argv, "--out") or (JOBS / ("matrix-" + time.strftime("%Y%m%d-%H%M%S"))))

    print(f"\n  smoke test · {mesh.name} · {len(fils)}×{len(layers)} real slices\n")
    rows, refused = [], []
    for fq in fils:
        for layer in layers:
            tag = f"{fq}@{layer}".replace(" ", "_").replace("/", "-")
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "run", str(mesh),
                                "--filament", fq, "--layer", layer,
                                "--out", str(root / tag), "--json"],
                               capture_output=True, text=True)
            try:
                v = json.loads(r.stdout)
            except json.JSONDecodeError:
                print(f"    ✗ {fq} @ {layer} mm — the run produced nothing readable")
                continue
            if not v.get("ok"):
                why = (v.get("refusals") or ["refused"])[0]
                if why not in [w for _, w in refused]:
                    refused.append((fq, why))
                print(f"    ✗ {fq} @ {layer} mm")
                continue
            rows.append((fq, layer, v["truth"]))
            print(f"    ✓ {fq} @ {layer} mm")

    for fq, why in refused:
        print(f"    🔴 {fq} — {why}")
    if not rows:
        print("\n  nothing sliced\n")
        return 1

    rows.sort(key=lambda r: r[2].get("minutes", 0))
    print(f"\n   {'filament':26s} {'layer':>6s} {'time':>9s} {'layers':>7s} {'grams':>7s} {'$':>6s}")
    for fq, layer, t in rows:
        mins = t.get("minutes", 0)
        print(f"   {fq[:26]:26s} {layer:>5s}m {mins//60:>6d}h{mins%60:02d} "
              f"{t.get('layers',0):>7d} {t.get('grams',0):>7.1f} {t.get('usd',0):>6.2f}")
    fast = rows[0]
    cheap = min(rows, key=lambda r: r[2].get("usd", 0) or 1e9)
    print(f"\n    fastest   {fast[0]} @ {fast[1]} mm — {fast[2]['minutes']//60}h"
          f"{fast[2]['minutes']%60:02d}")
    print(f"    cheapest  {cheap[0]} @ {cheap[1]} mm — ${cheap[2].get('usd','?')}")
    print(f"\n  every one of those is a file on disk under {root}\n")
    return 0


CMDS = {"run": cmd_run, "matrix": cmd_matrix, "layers": cmd_layers,
        "show": cmd_show, "filaments": P.cmd_filaments}

if __name__ == "__main__":
    if not CFG.orca_command():
        sys.exit("[slice] " + CFG.ORCA_MISSING)
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
