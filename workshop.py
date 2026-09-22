#!/usr/bin/env python3
"""
workshop.py — the whole gate stack behind one command, so a part can be tested the way a
visitor would meet it rather than by chaining five scripts by hand.

    intake  ->  geometry  ->  stance  ->  slice  ->  truth  ->  shelf  ->  a file to print

Each gate can refuse, and a refusal stops the ones after it: there is no point measuring the
balance of a mesh with holes in it, and no point slicing a part that is going to be turned
over first. What comes out the far end is a `.3mf` that opens in Bambu Studio already
correct — the right machine, the right plate, the right spool, the brim decision already
made — so the person at the machine looks at the verdict and presses print. That is the
release, and it is the only step ClaudIA does not do.

    workshop.py doctor              what is plugged in, what is missing, in blocking order
    workshop.py run <file|link>     every gate, one report, one file
    workshop.py selftest            drive known-good and known-bad shapes through the gates

Options for `run`:
  --filament <name>   which spool. Omit it and the shelf is asked what may be offered.
  --layer 0.20        layer height (default: the machine's own profile)
  --no-turn           measure the stance but leave the part as the author left it
  --json              the whole verdict, machine-readable
"""
import json, math, struct, subprocess, sys, time, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gates import config as CFG, profiles as P, mesh as S, stance as ST, intake as IN
from gates.refusal import Refusal

RUNS = CFG.RUNS / "workshop"
BAMBU_STUDIO = Path("/Applications/BambuStudio.app")

OK, WARN, STOP, SKIP = "✅", "⚠ ", "🔴", "· "


class Run:
    def __init__(self, src, argv):
        self.src, self.argv = src, argv
        self.dir = RUNS / time.strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gates, self.stopped = [], None
        self.pkey, self.prof, self.cfg = CFG.active()

    def gate(self, n, name, state, lines, data=None):
        self.gates.append({"n": n, "name": name, "state": state, "lines": lines,
                           "data": data or {}})
        if state == "stop" and not self.stopped:
            self.stopped = f"{n} · {name}"
        return state != "stop"

    def as_dict(self):
        return {"source": str(self.src), "printer": self.pkey, "dir": str(self.dir),
                "gates": self.gates, "stopped_at": self.stopped,
                "ok": self.stopped is None}


def run_gates(src, argv):
    r = Run(src, argv)

    # ---- 0 · intake -------------------------------------------------------------
    f = Path(src).expanduser().resolve()
    if f.suffix.lower() == ".3mf":
        try:
            names = IN.listing(f)
        except SystemExit as e:
            # A container the bench will not open is gate 0 refusing, not the report dying
            # half-written. (A typed refusal is the better shape for this and is proposed
            # separately; this keeps intake.py's own idiom until then.)
            r.gate("0", "intake", "stop", [str(e)])
            return r
        stls = IN.sanitize(f, r.dir / "mesh")
        mesh = stls[0]
        risky = [n for n in names if "gcode" in n.lower() or "settings" in n.lower()]
        lines = [f"{len(names)} parts in the container, none of them carried over — "
                 f"what is printed is the {len(stls)} STL we made from it"]
        if risky:
            lines.append(f"{len(risky)} of them could have held machine instructions "
                         f"({risky[0]}) and are gone")
        r.gate("0", "intake", "ok", lines)
    else:
        mesh = r.dir / f.name
        mesh.write_bytes(f.read_bytes())
        r.gate("0", "intake", "ok", [f"{f.suffix.lstrip('.').upper()}, "
                                     f"{f.stat().st_size/1e6:.1f} MB — a shape and nothing else"])

    # ---- 1a · geometry ----------------------------------------------------------
    tris = S.read_stl(mesh)
    if not tris:
        r.gate("1a", "geometry", "stop", ["no triangles in the file"])
        return r
    g = S.geometry(tris)
    rep = S.printability(g, r.prof, r.cfg, r.pkey)
    if rep["problems"]:
        r.gate("1a", "geometry", "stop" if not rep["ok"] else "warn", rep["problems"], rep)
    else:
        r.gate("1a", "geometry", "ok",
               [f"watertight, {g['tris']} triangles, "
                f"{rep['bbox'][0]:.0f}×{rep['bbox'][1]:.0f}×{rep['bbox'][2]:.0f} mm — fits the bed"],
               rep)
    if r.stopped:
        return r

    # ---- 1b · stance ------------------------------------------------------------
    m = ST.measure(tris)
    verdict, why, plan = ST.decide(m)
    if verdict == "turn" and "--no-turn" not in argv:
        turned = ST.reorient(mesh, r.dir / (mesh.stem + ".turned.stl"))
        if turned:
            m2 = ST.measure(S.read_stl(turned))
            v2, why2, plan2 = ST.decide(m2)
            if v2 == "print":
                # `before` is captured first on purpose: reassigning m and then reading it
                # for the "was" figure reports the after-value twice and every turn looks
                # like it gained nothing.
                before = m["contact_ratio"] * 100
                mesh, m, plan = turned, m2, plan2
                gained = m2["contact_ratio"] * 100 - before
                r.gate("1b", "stance", "warn",
                       why + [f"turned it over: {before:.0f}% → "
                              f"{m2['contact_ratio']*100:.0f}% of its shadow on the plate "
                              f"({gained:+.0f} points, and not a gram spent)"] + why2, m2)
            else:
                plan = {"brim": "on", "support": "auto", "raft": True}
                r.gate("1b", "stance", "warn",
                       why + ["turning it over did not find a face either — this one needs a "
                              "raft, and the bottom will carry the scar"], m2)
        else:
            r.gate("1b", "stance", "stop", why + ["and the slicer could not re-orient it"], m)
    else:
        r.gate("1b", "stance", "ok" if verdict == "print" else "stop", why, m)
    if r.stopped:
        return r

    # ---- the spool --------------------------------------------------------------
    fil = P._flag(argv, "--filament")
    if not fil:
        try:
            from gates import shelf
            good, _ = shelf.offer(0)
        except Refusal as e:
            r.gate("2", "slice", "stop",
                   [str(e), "or name one yourself with --filament"])
            return r
        if not good:
            r.gate("2", "slice", "stop", ["nothing on the shelf can take this job"])
            return r
        fil = good[0]["profile"]

    # ---- 2 + 3 · slice and truth ------------------------------------------------
    cmd = [sys.executable, "-m", "gates.slice", "run", str(mesh), "--filament", fil,
           "--brim", plan["brim"], "--out", str(r.dir / "slice"), "--json"]
    if P._flag(argv, "--layer"):
        cmd += ["--layer", P._flag(argv, "--layer")]
    if plan.get("raft"):
        cmd += ["--set", "raft_layers=3"]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    try:
        v = json.loads(out.stdout)
    except json.JSONDecodeError:
        r.gate("2", "slice", "stop", ["the slicer produced nothing readable",
                                      (out.stderr or "")[:200]])
        return r
    if not v.get("ok"):
        r.gate("2", "slice", "stop", v.get("refusals") or ["refused"], v)
        return r
    r.gate("2", "slice", "warn" if v.get("warnings") else "ok",
           [f"{v['filament']} on the {v['plate']}, brim {plan['brim']}"] + (v.get("warnings") or []),
           v)

    t = v["truth"]
    r.gate("3", "truth", "ok",
           [f"{t['minutes']//60}h {t['minutes']%60:02d}m · {t['grams']} g"
            + (f" · ${t['usd']}" if "usd" in t else "")
            + f" · {t['layers']} layers to {t['max_z_mm']} mm",
            f"nozzle {t.get('nozzle_c','?')} C, first-layer bed {t.get('bed_c','?')} C"], t)

    # ---- the shelf, now that the grams are real ---------------------------------
    try:
        from gates import shelf
        good, excluded = shelf.offer(t["grams"])
        mine = [x for x in good if x["profile"] == v["filament"]]
        if mine:
            r.gate("s", "shelf", "ok",
                   [f"{mine[0]['grams_left']} g left on that spool, job needs {t['grams']:.0f} g"]
                   + (mine[0].get("warnings") or []))
        else:
            gone = [x for x in excluded if x["profile"] == v["filament"]]
            r.gate("s", "shelf", "warn",
                   [gone[0]["excluded"] if gone else
                    "that profile is not on the shelf — nothing is loaded with it"])
    except Refusal as e:
        r.gate("s", "shelf", "skip", [" ".join(str(e).split())])

    r.job = v["job"]["3mf"]
    return r


def show(r):
    print(f"\n  {Path(r.src).name}  ·  {r.pkey}\n")
    icon = {"ok": OK, "warn": WARN, "stop": STOP, "skip": SKIP}
    for g in r.gates:
        print(f"   {icon[g['state']]} {g['n']:>2s} · {g['name']:9s} {g['lines'][0] if g['lines'] else ''}")
        for l in g["lines"][1:]:
            print(f"        {' '*12}{l}")
    if r.stopped:
        print(f"\n  🔴 STOPPED AT {r.stopped.upper()} — nothing was printed.\n")
        return 1
    print(f"\n  ✅ READY\n     {r.job}\n")
    if BAMBU_STUDIO.exists():
        print(f"     Open it in Bambu Studio and press print. Nothing else needs changing.\n")
    else:
        print(f"     Bambu Studio is not installed on this Mac, so there is nowhere to press\n"
              f"     print from. That is the last missing piece of the release step.\n")
    return 0


# ---------- doctor ----------

def cmd_doctor(_argv):
    """In blocking order: the thing that stops you first is listed first."""
    checks = []
    orca, where = CFG.find_orca()
    checks.append(("OrcaSlicer", bool(orca),
                   "the slicer behind gates 1b, 2 and 3"
                   + (f" — found at {where}" if orca else " — not found anywhere we looked"),
                   CFG.ORCA_MISSING))
    profiles = CFG.orca_profiles()
    checks.append(("profile database", bool(profiles),
                   "the vendor's own temperatures and nozzle rules"
                   + (f" — {profiles}" if profiles else " — not beside the slicer"),
                   CFG.PROFILES_MISSING))
    checks.append(("Bambu Studio", BAMBU_STUDIO.exists(),
                   "where the router presses print — the release step has no other home",
                   "download it from bambulab.com; Handy cannot send an arbitrary .3mf"))
    e = CFG.shelf_endpoint()
    fs = ""
    if e:
        import os
        fs = os.environ.get(e.get("secret_env") or "SHELF_SECRET") or (
            subprocess.run(e["secret_command"], capture_output=True, text=True).stdout.strip()
            if e.get("secret_command") else "")
    checks.append(("shelf", bool(e) and bool(fs),
                   "without it the filament has to be named by hand every time",
                   "cp config/shelf.example.json config/shelf.json, then set its passphrase"
                   if not e else "the endpoint is configured but the passphrase is not set"))
    conf = CFG.DIR / "bambu.json"
    ac = ""   # the LAN lane is optional and lives outside this repo — see docs/the-router.md
    checks.append(("printer on the LAN", conf.exists() and bool(ac),
                   "only for the auto-send lane, which is not on the critical path",
                   "python3 bambu.py setup"))

    print("\n  the workshop · what is plugged in\n")
    for name, ok, why, fix in checks:
        print(f"    {OK if ok else STOP} {name:20s} {why}")
        if not ok:
            # A fix can be several lines. They are written with the gutter a `sys.exit` needs,
            # so strip that and re-indent to this column rather than printing two indents.
            head, *rest = fix.splitlines()
            print(f"       {' '*20} → {head}")
            for line in rest:
                print(f"       {' '*20}   {line[8:] if line.startswith(' ' * 8) else line.lstrip()}")
    blocking = [c for c in checks[:-1] if not c[1]]   # everything but the LAN lane
    print(f"\n  {'READY TO TEST' if not blocking else str(len(blocking)) + ' thing(s) in the way'}\n")
    return 0 if not blocking else 1


# ---------- selftest ----------

def _stl(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for v in (a, b, c):
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")


def _cone(r, h, n=64, on_tip=False):
    ring = [(r * math.cos(2*math.pi*i/n), r * math.sin(2*math.pi*i/n)) for i in range(n)]
    apex = (0, 0, 0) if on_tip else (0, 0, h)
    zc = h if on_tip else 0
    t = []
    for i in range(n):
        p, q = ring[i], ring[(i+1) % n]
        t += [(apex, (p[0], p[1], zc), (q[0], q[1], zc)),
              ((0, 0, zc), (q[0], q[1], zc), (p[0], p[1], zc))]
    return t


def _box(w, d, h, skip_top=False):
    v = [(0,0,0),(w,0,0),(w,d,0),(0,d,0),(0,0,h),(w,0,h),(w,d,h),(0,d,h)]
    q = [(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)]
    if skip_top:
        q = q[:1] + q[2:]                     # a hole where the lid should be
    t = []
    for a, b, c, d_ in q:
        t += [(v[a], v[b], v[c]), (v[a], v[c], v[d_])]
    return t


def _resolver_cases():
    """The slicer resolver, checked on whatever machine this is. It earns its own cases
    because it is the one piece of the stack that has to behave on a bench where OrcaSlicer
    is NOT installed: the answer there is a sentence somebody can act on, and the way to
    get that wrong is a traceback five frames into subprocess."""
    import os, tempfile
    cases = []

    keep = os.environ.get(CFG.ORCA_ENV)
    try:
        fake = Path(tempfile.mkdtemp(prefix="selftest-orca-")) / "orca-slicer"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        os.environ[CFG.ORCA_ENV] = str(fake)
        cmd, where = CFG.find_orca()
        cases.append((f"${CFG.ORCA_ENV} wins", cmd == [str(fake)] and where == "$" + CFG.ORCA_ENV))
    finally:
        os.environ.pop(CFG.ORCA_ENV, None)
        if keep is not None:
            os.environ[CFG.ORCA_ENV] = keep

    # The refusal has to name both halves — what to install, and what to set when it is
    # installed somewhere this did not look. A sentence missing either one sends somebody
    # to the source to find out what the variable is called.
    cases.append(("the refusal names install and override",
                  "brew install" in CFG.ORCA_MISSING and "flatpak install" in CFG.ORCA_MISSING
                  and CFG.ORCA_ENV in CFG.ORCA_MISSING))
    return cases


# What `python3 -m gates.shelf list` printed to stderr before there was a Refusal type, and
# what it has to go on printing. A bench with no inventory service runs into this on its
# first day, so it is the refusal most worth holding still.
SHELF_UNCONFIGURED = (
    "[shelf] no shelf configured.\n"
    "        cp config/shelf.example.json config/shelf.json  and fill it in.\n"
    "        Without it, name the filament yourself with --filament.")


def _refusal_cases():
    """A refusal raised inside a library function has to arrive as a value a caller can
    read, and still reach a terminal as the same text and the same exit code. Both halves
    are checked, because it is the second half that nobody notices breaking."""
    import os, tempfile
    cases = []

    r = Refusal("selftest.shape", "the sentence.", "the first fix line\nthe second")
    cases.append(("a refusal rebuilds the text sys.exit printed",
                  r.text() == "the sentence.\n        the first fix line\n        the second"))
    cases.append(("and is not a SystemExit any more", not isinstance(r, SystemExit)))

    d = Path(tempfile.mkdtemp(prefix="selftest-refusal-"))
    got = []
    for name, arg in (("stance.no_file", d / "nothing.stl"),
                      ("stance.not_a_mesh", d / "notes.txt")):
        if arg.suffix == ".txt":
            arg.write_text("not a mesh\n")
        try:
            ST._load(arg)
            got.append(None)
        except Refusal as e:
            got.append(e.code)
        except SystemExit:
            got.append("SystemExit")
    cases.append((f"stance._load raises {got[0]} and {got[1]}",
                  got == ["stance.no_file", "stance.not_a_mesh"]))

    # The terminal half, run as a terminal runs it: a config directory with no shelf.json in
    # it, which is the state every fresh clone is in.
    env = dict(os.environ, COMMONS_PRINT_CONFIG=str(d / "config"))
    (d / "config").mkdir(exist_ok=True)
    out = subprocess.run([sys.executable, "-m", "gates.shelf", "list"],
                         capture_output=True, text=True, cwd=HERE, env=env)
    cases.append(("the shelf CLI says it the same way, and still exits 1",
                  out.returncode == 1 and out.stderr.rstrip("\n") == SHELF_UNCONFIGURED))
    return cases


def _3mf(path, entries):
    """A container built in code. Committing a crafted .3mf as a binary would mean asking
    everyone to trust a zip bomb sitting in the repo; a shape built at test time can be read
    in the diff instead."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in entries:
            z.writestr(name, blob)
    return path


def _container_cases():
    """What gate 0 must refuse before the slicer is handed a file, and — the case that keeps
    the other four honest — what it must still accept."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="selftest-3mf-"))
    model = [("3D/3dmodel.model", b"<model unit=\"millimeter\"/>"),
             ("Metadata/model_settings.config", b"<config/>")]

    cases = [
        ("a plain model container is accepted", model, True),
        ("a container with a sliced plate",
         model + [("Metadata/plate_1.gcode", b"G1 X0 Y0 E1 F1200\n" * 200)], False),
        ("a container with a post-processing command",
         model + [("Metadata/project_settings.config",
                   json.dumps({"post_process": ["/bin/echo ran on the bench"]}).encode())], False),
        ("a container with an executable in it",
         model + [("Auxiliaries/setup.sh", b"#!/bin/sh\nexit 0\n")], False),
        # 20 MB of zeros is 19 KB on disk and 1028:1. It never reaches the 800 MB cap, so
        # the cap alone would hand it to the slicer and let that be the thing that runs out
        # of memory.
        ("a zip bomb, 20 MB out of 19 KB",
         model + [("3D/Objects/object_1.model", b"\0" * 20_000_000)], False),
        ("an entry that unpacks outside the container",
         model + [("../escape.model", b"x")], False),
    ]

    out = []
    for name, entries, want_ok in cases:
        f = _3mf(d / (name.replace(" ", "-").replace(",", "") + ".3mf"), entries)
        t0 = time.time()
        with zipfile.ZipFile(f) as z:
            bad = IN.contraband(z)
        ms = (time.time() - t0) * 1000
        got_ok = not bad
        detail = "" if got_ok else f"  ({bad[0][1][:54]}…)"
        out.append((f"{name} · {ms:.1f} ms{detail}", got_ok == want_ok))
    return out


def cmd_selftest(argv):
    """Known shapes with known answers. Each case names the gate that must fire, so a gate
    that quietly stops refusing anything shows up here rather than on the plate."""
    d = RUNS / "selftest"
    d.mkdir(parents=True, exist_ok=True)
    cases = [
        ("a lidless box",     _box(40, 30, 20, skip_top=True), "1a", "stop"),
        ("a cone on its tip", _cone(25, 60, on_tip=True),      "1b", "warn"),
        ("a 12:1 tower",      _box(10, 10, 120),               "1b", "ok"),
        ("a flat plate",      _box(80, 60, 4),                 "1b", "ok"),
    ]
    fil = P._flag(argv, "--filament") or "Bambu PLA Basic"
    print(f"\n  selftest · {len(cases)} shapes with known answers · {fil}\n")
    bad = 0
    for name, tris, gate_n, want in cases:
        f = d / (name.replace(" ", "-") + ".stl")
        _stl(f, tris)
        r = run_gates(f, ["--filament", fil])
        got = next((g["state"] for g in r.gates if g["n"] == gate_n), "never ran")
        hit = got == want
        bad += 0 if hit else 1
        print(f"    {OK if hit else STOP} {name:20s} gate {gate_n} {got:5s} "
              f"(wanted {want})")
        if hit and gate_n == "1b" and got != "stop":
            t = next((g["data"] for g in r.gates if g["n"] == "3"), {})
            if t:
                print(f"       {' '*20} sliced: {t['minutes']//60}h{t['minutes']%60:02d}, "
                      f"{t['grams']} g")
        for g in r.gates:
            if g["state"] == "stop":
                print(f"       {' '*20} {g['lines'][0][:90]}")
    cmd, where = CFG.find_orca()
    print(f"\n  the slicer · {where if cmd else 'not found — the four shapes above cannot slice'}\n")
    for name, hit in _resolver_cases():
        bad += 0 if hit else 1
        print(f"    {OK if hit else STOP} {name}")
    print("\n  refusals · the same sentences, carried rather than exited\n")
    for name, hit in _refusal_cases():
        bad += 0 if hit else 1
        print(f"    {OK if hit else STOP} {name}")
    print("\n  containers · what gate 0 opens, and what it will not\n")
    for name, hit in _container_cases():
        bad += 0 if hit else 1
        print(f"    {OK if hit else STOP} {name}")

    print(f"\n  {'✅ every gate fired where it should' if not bad else STOP + f' {bad} check(s) did not behave'}\n")
    return 1 if bad else 0


def cmd_run(argv):
    if not argv:
        sys.exit("usage: workshop.py run <file|makerworld-link> [--filament <name>]")
    src = argv[0]
    if src.startswith("http") or src.isdigit():
        return IN.cmd_link(argv)
    r = run_gates(src, argv)
    (r.dir / "run.json").write_text(json.dumps(r.as_dict(), indent=1) + "\n")
    if "--json" in argv:
        print(json.dumps(r.as_dict(), indent=1))
        return 0 if not r.stopped else 1
    return show(r)


CMDS = {"run": cmd_run, "doctor": cmd_doctor, "selftest": cmd_selftest}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
