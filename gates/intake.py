#!/usr/bin/env python3
"""
intake.py — gate 0. Where somebody else's design comes in, and where it stops being
somebody else's instructions.

TWO KINDS OF ARRIVAL, and they are not symmetrical.

  A LINK is public. MakerWorld will tell anyone, with no account and no key, everything
  about a design except the file: the title, the licence, and every print profile on it
  with the machine it was cut for, the nozzle, the filament, the grams and the seconds.
  That is the whole spec sheet, free, and it is what we check.

  THE FILE is not public. `Please log in to download models.` — MakerWorld's own words at
  a 403. So the file comes out of *their* account, by their hand, and lands here as an
  upload. We never hold a MakerWorld session any more than we hold a Bambu one. What the
  link buys is knowing, before they go and fetch it, which of the three profiles on that
  page is the right one and whether it can run here at all.

WHY THE PROFILE IS A HINT AND NEVER A VERDICT. A MakerWorld profile is a claim its author
made about their own machine. Three ways it goes wrong, all of them seen in one real
design on the first day of looking:
  * it is cut for another printer — primary compatibility A1, ours only in the also-runs
    list. Different plate, different chamber, open frame against enclosed.
  * the title disagrees with the file. `PETG profile`, whose machine-readable filament
    list says PLA. Somebody duplicated a profile, renamed it, and shipped it.
  * the numbers are rounded to uselessness. `weight: 1`, `usedG: 1` for a real part.
So the profile decides nothing. It tells us what to expect and which file to ask for; the
answer still comes from slice.py cutting it against this machine and this shelf.

AND THE RULE THAT MAKES A SHARED NOZZLE SAFE. A `.3mf` is a zip, and a Bambu project `.3mf`
carries `Metadata/project_settings.config` — which holds `machine_start_gcode`. That is
arbitrary G-code: it can drive the head, override temperatures, skip a check. Nobody
reviewing the plate in Bambu Studio can see it, so an owner pressing print is not a review.
Therefore: **we never print a file that arrived. We print a file we sliced.** Intake keeps
`3D/` — the mesh — and throws every other part of the container away, and slice.py rebuilds
the settings from our own profiles. A file that was already perfect gets re-sliced anyway.

Usage:
  intake.py link  <url|id>              read the specs off a MakerWorld link
  intake.py strip <file.3mf> [-o out]   keep the mesh, discard the instructions
  intake.py open  <file|url> [--slice --filament <name>]   the whole gate, end to end
"""
import io, json, re, shutil, subprocess, sys, time, zipfile
from pathlib import Path

from . import config as CFG

from . import profiles as P

QUEUE = CFG.RUNS / "queue"
MW_API = "https://makerworld.com/api/v1/design-service/design/{}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MESH_OK = {".stl", ".3mf", ".step", ".stp", ".obj"}
MAX_FILE_MB = 200          # the upload cap
MAX_UNPACKED_MB = 800      # what a .3mf is allowed to become. A zip bomb dies here.

# printers.json calls it X1C; MakerWorld calls it "X1 Carbon". Its own devModelName is the
# authority when it is there — BL-P001 is the X1C's model id in Bambu's own machine profile.
MW_NAME = {"X1C": ("X1 Carbon", "BL-P001"), "X1": ("X1", "BL-P002"), "X1E": ("X1E", "C13"),
           "P1S": ("P1S", "C12"), "P1P": ("P1P", "C11"), "A1": ("A1", "N2S"),
           "A1M": ("A1 mini", "N1"), "H2D": ("H2D", "O1D")}

# Licences that constrain what a hub may do with someone else's model. None of these stop a
# person printing their own copy; they matter the moment the hub charges for it or ships it
# on, so they are surfaced rather than enforced.
LICENCE_NOTE = {
    "nc": "non-commercial — fine to print for the person who brought it, not to sell",
    "nd": "no derivatives — print it as it is, do not remix and republish",
    "exclusive": "MakerWorld Exclusive — the file stays on MakerWorld; do not redistribute it",
}


# ---------- the link ----------

def design_id(s):
    s = s.strip()
    if s.isdigit():
        return s
    m = re.search(r"/models/(\d+)", s) or re.search(r"[?&]id=(\d+)", s)
    if not m:
        sys.exit(f"[intake] not a MakerWorld model link: {s}\n"
                 f"          expected something like https://makerworld.com/en/models/3256319-slug")
    return m.group(1)


def fetch(url):
    """Through curl rather than urllib. The system python on this Mac has no CA bundle wired
    up (`CERTIFICATE_VERIFY_FAILED` on every https call), curl uses the OS trust store, and
    the alternative is a pip dependency in a tree that has stayed stdlib-only on purpose."""
    r = subprocess.run(["curl", "-sS", "--max-time", "25", "-A", UA,
                        "-H", "Accept: application/json", "-w", "\n%{http_code}", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[intake] could not reach MakerWorld: {r.stderr.strip()}")
    body, _, code = r.stdout.rpartition("\n")
    if code.strip() != "200":
        sys.exit(f"[intake] MakerWorld answered {code.strip()} for {url}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit("[intake] MakerWorld did not answer with JSON — the endpoint may have moved")


def licence_flags(lic):
    l = (lic or "").lower()
    out = []
    if "nc" in l.split("-") or "noncommercial" in l or "non-commercial" in l:
        out.append(LICENCE_NOTE["nc"])
    if "nd" in l.split("-") or "noderiv" in l:
        out.append(LICENCE_NOTE["nd"])
    if "exclusive" in l:
        out.append(LICENCE_NOTE["exclusive"])
    return out


def read_profiles(d, prof):
    """Every print profile on the design, judged against the machine we actually have."""
    want_name, want_id = MW_NAME.get(prof.get("model", ""), (prof.get("model", ""), ""))
    want_noz = float(prof.get("nozzle", 0.4))
    rows = []
    for i in d.get("instances") or []:
        info = (i.get("extention") or {}).get("modelInfo") or {}
        compat = [info.get("compatibility") or {}] + (info.get("otherCompatibility") or [])
        compat = [c for c in compat if c]
        primary = (info.get("compatibility") or {}).get("devProductName", "?")
        match = next((c for c in compat
                      if (c.get("devModelName") == want_id
                          or c.get("devProductName") == want_name)
                      and float(c.get("nozzleDiameter") or 0) == want_noz), None)
        fils = []
        try:
            fils = json.loads(i.get("instanceFilaments") or "[]") if isinstance(
                i.get("instanceFilaments"), str) else (i.get("instanceFilaments") or [])
        except Exception:
            pass
        types = sorted({(f.get("type") or "?").upper() for f in fils})
        grams = sum(float(f.get("usedG") or 0) for f in fils)
        secs = i.get("prediction") or 0
        row = {"id": i.get("id"), "title": i.get("title") or "", "runs_here": bool(match),
               "primary": primary, "on": [c.get("devProductName") for c in compat],
               "nozzle": (info.get("compatibility") or {}).get("nozzleDiameter"),
               "materials": types, "grams_claimed": round(grams, 1),
               "minutes_claimed": round(secs / 60) if secs else None,
               "needs_ams": bool(i.get("needAms")), "colours": i.get("materialColorCnt"),
               "is_default": bool(i.get("isDefault"))}
        t = row["title"].upper()
        named = [m for m in ("PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PA") if m in t]
        if named and types and not set(named) & set(types):
            row["contradiction"] = (f"the profile is called {row['title']!r} but its filament "
                                    f"list says {', '.join(types)} — trust the list, not the name")
        rows.append(row)
    return rows


def cmd_link(argv):
    if not argv:
        sys.exit("usage: intake.py link <makerworld url or id>")
    printers = json.loads(CFG.PRINTERS.read_text())
    prof = printers["profiles"][printers["active"]]
    d = fetch(MW_API.format(design_id(argv[0])))
    if not d.get("id"):
        sys.exit("[intake] MakerWorld has no design at that id")

    rows = read_profiles(d, prof)
    lic = d.get("license") or "not stated"
    out = {"source": "makerworld", "id": d["id"], "title": d.get("title"),
           "author": (d.get("designCreator") or {}).get("name"),
           "url": f"https://makerworld.com/en/models/{d['id']}-{d.get('slug','')}",
           "licence": lic, "licence_notes": licence_flags(lic),
           "printer": printers["active"], "profiles": rows,
           "runs_here": [r for r in rows if r["runs_here"]]}

    if "--json" in argv:
        print(json.dumps(out, indent=1))
        return 0

    print(f"\n  {out['title']}\n  by {out['author']} · {lic}")
    for n in out["licence_notes"]:
        print(f"    ·  {n}")
    print(f"\n  {len(rows)} print profile(s), against {printers['active']} "
          f"({prof.get('nozzle')} mm):\n")
    for r in rows:
        mark = "✅" if r["runs_here"] else "🚫"
        print(f"   {mark} {r['title'] or 'untitled':28.28s} "
              f"{'/'.join(r['materials']) or '?':10.10s} "
              f"{str(r['grams_claimed'])+' g':>8s} "
              f"{(str(r['minutes_claimed'])+' min') if r['minutes_claimed'] else '—':>9s}"
              f"{'  AMS' if r['needs_ams'] else ''}")
        if not r["runs_here"]:
            print(f"      cut for {r['primary']} at {r['nozzle']} mm; this machine is not on its list")
        if r.get("contradiction"):
            print(f"      ⚠  {r['contradiction']}")
    print(f"\n  The numbers above are the author's claim about their own machine. They are\n"
          f"  what to expect, not what will happen — slice.py decides that here.\n")
    if out["runs_here"]:
        print(f"  Next: download the marked profile from your own MakerWorld account —\n"
              f"  {out['url']}\n  — and bring the file back:  intake.py open <file> --slice "
              f"--filament \"...\"\n")
    else:
        print(f"  Nothing on this page was cut for {printers['active']}. The mesh may still be\n"
              f"  fine — bring the STL and we cut it ourselves.\n")
    return 0 if out["runs_here"] else 1


# ---------- the file ----------

def listing(src: Path):
    """What is inside, by name only, so the report can say what was thrown away. Names are
    read; nothing in the archive is executed, and nothing is extracted."""
    unpacked, names = 0, []
    with zipfile.ZipFile(src) as z:
        for i in z.infolist():
            unpacked += i.file_size
            if unpacked > MAX_UNPACKED_MB * 1024 * 1024:
                sys.exit(f"[intake] this .3mf unpacks to more than {MAX_UNPACKED_MB} MB — refused")
            if not i.is_dir():
                names.append(i.filename)
    return names


def sanitize(src: Path, outdir: Path):
    """A .3mf in, plain STL out, converted by the slicer's own loader.

    The first version of this did zip surgery — keep `3D/`, drop `Metadata/`. It produced a
    container OrcaSlicer would not open at all: a Bambu `.3mf` keeps its object transforms in
    `Metadata/model_settings.config`, so a 3mf with the metadata cut out is a 3mf with no
    objects placed on the plate. Reassembling it by hand means owning a format Bambu changes
    at will.

    So we let the slicer read it and write the geometry back out as STL instead. STL has
    nowhere to put an instruction — no settings, no start G-code, no plate config, only
    triangles — which makes the discard total rather than careful. `--export-stl` is the
    flag that works; `--export-stls` silently produces nothing.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    orca = CFG.orca_command()
    if not orca:
        sys.exit("[intake] " + CFG.ORCA_MISSING)
    r = subprocess.run(orca + ["--datadir", str(outdir / "dd"), "--debug", "2",
                               "--logfile", str(outdir / "convert.log"),
                               "--export-stl", "--outputdir", str(outdir), str(src)],
                       capture_output=True, text=True, timeout=600)
    stls = sorted(outdir.rglob("*.stl"))
    if not stls:
        sys.exit(f"[intake] the slicer could not read {src.name} — it is not a model file we "
                 f"can gate. See {outdir/'convert.log'}")
    return stls


def cmd_strip(argv):
    if not argv:
        sys.exit("usage: intake.py strip <file.3mf> [-o outdir]")
    src = Path(argv[0]).expanduser().resolve()
    out = Path(P._flag(argv, "-o") or src.with_name(src.stem + ".mesh")).expanduser()
    names = listing(src)
    stls = sanitize(src, out)
    risky = [n for n in names if "gcode" in n.lower() or "settings" in n.lower()
             or "plate_" in n.lower()]
    print(f"\n  {src.name}  ->  {len(stls)} STL(s) in {out}")
    print(f"    the container held {len(names)} part(s); none of them survive the conversion")
    if risky:
        print(f"    🔴 {len(risky)} of them could carry machine instructions:")
        for n in risky[:6]:
            print(f"       {n}")
    for f in stls:
        print(f"    ·  {f.name}  ({f.stat().st_size/1e6:.2f} MB)")
    print()
    return 0


def cmd_open(argv):
    """The whole gate: take a link or a file, end with something slice.py can cut."""
    if not argv:
        sys.exit("usage: intake.py open <file|url> [--slice --filament <name>]")
    src = argv[0]
    if src.startswith("http") or src.isdigit():
        return cmd_link(argv)

    f = Path(src).expanduser().resolve()
    if not f.exists():
        sys.exit(f"[intake] no such file: {f}")
    if f.suffix.lower() not in MESH_OK:
        sys.exit(f"[intake] {f.suffix} is not a shape. Accepted: {', '.join(sorted(MESH_OK))}")
    mb = f.stat().st_size / 1e6
    if mb > MAX_FILE_MB:
        sys.exit(f"[intake] {mb:.0f} MB is over the {MAX_FILE_MB} MB cap")

    job = QUEUE / time.strftime("%Y%m%d-%H%M%S")
    job.mkdir(parents=True, exist_ok=True)
    if f.suffix.lower() == ".3mf":
        names = listing(f)
        stls = sanitize(f, job / "mesh")
        mesh = stls[0]
        risky = [n for n in names if "gcode" in n.lower() or "settings" in n.lower()]
        print(f"\n  gate 0 — {f.name}")
        print(f"    {len(names)} part(s) in the container, discarded; "
              f"{len(stls)} STL(s) kept as the only thing that carries over")
        if risky:
            print(f"    🔴 {len(risky)} of them could have carried machine instructions:")
            for r in risky[:4]:
                print(f"       {r}")
        if len(stls) > 1:
            print(f"    ⚠  {len(stls)} objects — only the first is queued for now")
    else:
        mesh = job / f.name
        shutil.copy2(f, mesh)
        print(f"\n  gate 0 — {f.name}  ({mb:.1f} MB, accepted)")

    rec = {"received": time.strftime("%Y-%m-%dT%H:%M:%S"), "original": str(f),
           "mesh": str(mesh), "size_mb": round(mb, 2), "state": "gated"}
    (job / "job.json").write_text(json.dumps(rec, indent=1) + "\n")
    print(f"    mesh    {mesh}")
    sys.stdout.flush()

    if "--slice" in argv:
        fil = P._flag(argv, "--filament")
        if not fil:
            sys.exit("[intake] --slice needs --filament")
        print()
        return subprocess.call([sys.executable, str(Path(__file__).parent / "slice.py"),
                                "run", str(mesh), "--filament", fil,
                                "--out", str(job / "slice")]
                               + (["--orient", "auto"] if "--orient" in argv else []))
    print(f"\n  Not sliced. Next:  slice.py run {mesh} --filament \"...\"\n")
    return 0


CMDS = {"link": cmd_link, "strip": cmd_strip, "open": cmd_open}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
