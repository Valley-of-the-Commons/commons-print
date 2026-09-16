#!/usr/bin/env python3
"""
shelf.py — what is actually loaded, and therefore what a visitor is allowed to choose.

THE POINT. "They do not know the proper filament" is only a question if you ask it. So the
interface never asks. It offers, and the offer is an intersection of three things that are
all knowable without troubling anybody:

    on the shelf right now   ∩   safe on this machine   ∩   enough grams left for this job

The first comes from the spool inventory in Supabase (`fs_list`), which is the same list
the phone writes to when a spool is logged or its remaining percentage is nudged. The second
is profiles.py reading the vendor's own fields — nozzle hardness, hotend ceiling, plate
compatibility, flow. The third needs gate 3 to have run, because only the sliced G-code
knows how many grams the job really takes.

What comes out is a short list of things that are physically present and known-safe,
described in words a person has — "tough and slightly flexible", "for something that lives
outside" — rather than in acronyms. There is no free-text filament field anywhere in the
public lane. There is no way to pick PA6-CF against a stainless nozzle, because it is not
on the list.

ORDERING. The grams test needs the slice, and the slice needs a filament. So the flow is:
offer the safe set -> slice against the one they chose -> confirm it still fits on that
spool -> only then queue it. `offer` without --grams answers the first question; `offer
--job <verdict.json>` answers the third.

THE ENDPOINT AND THE SECRET. Neither is in this repo. `config/shelf.json` (gitignored)
names your inventory service; the passphrase comes from an environment variable or from a
command the config names — a keychain reader, `pass`, `op read`. Nothing here prints it and
nothing here writes it down. A bench with no shelf configured still runs every other gate.

Usage:
  shelf.py list                        every spool, with what is left on it
  shelf.py offer [--grams 40]          what a visitor may choose from
  shelf.py offer --job <verdict.json>  the same, filtered by what the job actually needs
"""
import json, os, subprocess, sys
from pathlib import Path

from . import config as CFG

from . import profiles as P

from .refusal import Refusal, cli

def endpoint():
    """No project reference is committed here. `config/shelf.json` names your own, and it
    is gitignored — see `config/shelf.example.json` for the shape. A repo without one still
    runs every other gate; it just has to be told the filament by name."""
    e = CFG.shelf_endpoint()
    if not e:
        raise Refusal("shelf.unconfigured", "[shelf] no shelf configured.",
                      "cp config/shelf.example.json config/shelf.json  and fill it in.\n"
                      "Without it, name the filament yourself with --filament.")
    return e

# What a material is FOR, in words somebody who has never printed can choose between. The
# acronym stays in the row; it is not what the question is asked in.
IN_WORDS = {
    "PLA":  "stiff and easy — the default, and wrong only outdoors or near heat",
    "PETG": "tougher and a little flexible, survives sun and water",
    "TPU":  "genuinely rubbery — bends and springs back",
    "ABS":  "impact-resistant, needs the door shut the whole way through",
    "ASA":  "like ABS but made for sunlight — outdoor parts that must not go chalky",
    "PC":   "very strong and heat-resistant; fussy, and it must be dry",
    "PA":   "nylon — tough and slippery, drinks water out of the air",
    "PVA":  "dissolves in water; it is support material, not a part",
}


def secret(e):
    """From the environment first, then from whatever command the config names — a keychain
    reader, `pass`, `op read`, anything that prints one line. Never from a file in the repo."""
    s = os.environ.get(e.get("secret_env") or "SHELF_SECRET")
    if s:
        return s.strip()
    cmd = e.get("secret_command")
    if cmd:
        s = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    if not s:
        raise Refusal("shelf.no_passphrase", "[shelf] no shelf passphrase.",
                      f"set ${e.get('secret_env','SHELF_SECRET')}, or make "
                      f"`{' '.join(cmd) if cmd else '<secret_command>'}` print one.")
    return s


def rpc(fn=None, body=None):
    e = endpoint()
    fn = fn or e.get("rpc", "fs_list")
    payload = json.dumps({"p_secreto": secret(e), **(body or {})})
    r = subprocess.run(["curl", "-sS", "--max-time", "20", "-X", "POST",
                        f"{e['url']}/rest/v1/rpc/{fn}",
                        "-H", f"apikey: {e['publishable_key']}",
                        "-H", f"Authorization: Bearer {e['publishable_key']}",
                        "-H", "Content-Type: application/json",
                        "-w", "\n%{http_code}", "-d", payload],
                       capture_output=True, text=True)
    body_s, _, code = r.stdout.rpartition("\n")
    if code.strip() == "401":
        raise Refusal("shelf.key_refused",
                      "[shelf] Supabase refused the publishable key — the project or the key moved")
    if code.strip() != "200":
        try:
            msg = json.loads(body_s).get("message", body_s)
        except Exception:
            msg = body_s
        if msg == "nope":
            raise Refusal("shelf.wrong_passphrase", "[shelf] the shelf passphrase is wrong")
        raise Refusal("shelf.unreachable", f"[shelf] {fn} answered {code.strip()}: {msg}")
    return json.loads(body_s)


def grams_left(r):
    return (r.get("spool_g") or 0) * (r.get("remaining_pct") or 0) / 100.0


def rows():
    return [r for r in rpc() if r.get("active", True)]


def offer(need_g=0.0, nozzle_material=None):
    """The intersection. Every exclusion carries its reason, because a chooser that silently
    drops an option teaches nobody anything — least of all the person holding the spool."""
    printers = json.loads(CFG.PRINTERS.read_text())
    prof = printers["profiles"][printers["active"]]
    nmat = nozzle_material or P.DEFAULT_NOZZLE_MATERIAL
    db = P._load()

    good, out = [], []
    for r in rows():
        left = grams_left(r)
        row = {"id": r.get("id"), "brand": r.get("brand"), "material": r.get("material"),
               "colour": r.get("color_name"), "hex": r.get("color_hex"),
               "profile": r.get("profile"), "grams_left": round(left),
               "location": r.get("location"), "words": IN_WORDS.get(
                   (r.get("material") or "").upper().split("-")[0], "")}
        if not r.get("profile"):
            row["excluded"] = "no slicer profile recorded for this spool — log it again"
            out.append(row)
            continue
        res = P.resolve(db, r["profile"])
        if not res:
            row["excluded"] = f"the profile {r['profile']!r} is not in the slicer's database"
            out.append(row)
            continue
        stop, warn, notes, plates = P.audit(res, prof.get("nozzle", 0.4), nmat, prof)
        row["warnings"] = warn
        if stop:
            row["excluded"] = stop[0]
            out.append(row)
            continue
        if need_g and left < need_g:
            row["excluded"] = (f"{left:.0f} g left, the job needs {need_g:.0f} g — a spool "
                               f"running out mid-print is a wasted plate")
            out.append(row)
            continue
        if need_g and left < need_g * 1.15:
            row["warnings"] = (row.get("warnings") or []) + [
                f"{left:.0f} g left against {need_g:.0f} g needed — that is under 15% to spare"]
        good.append(row)
    good.sort(key=lambda r: (-(r["grams_left"]), r["material"] or ""))
    return good, out


def cmd_list(argv):
    rs = rows()
    if "--json" in argv:
        print(json.dumps(rs, indent=1))
        return 0
    total = sum(grams_left(r) for r in rs)
    print(f"\n  {len(rs)} spool(s) on the shelf · {total/1000:.2f} kg left\n")
    for r in sorted(rs, key=lambda x: -grams_left(x)):
        low = "  ⚠ running low" if (r.get("remaining_pct") or 0) <= 30 else ""
        print(f"    {(r.get('material') or '?'):8s} {(r.get('brand') or ''):14.14s} "
              f"{(r.get('color_name') or ''):16.16s} {grams_left(r):5.0f} g "
              f"({r.get('remaining_pct')}%)  {r.get('location') or ''}{low}")
    print()
    return 0


def cmd_offer(argv):
    need = float(P._flag(argv, "--grams") or 0)
    job = P._flag(argv, "--job")
    if job:
        v = json.loads(Path(job).expanduser().read_text())
        need = max(need, (v.get("truth") or {}).get("grams") or 0)
    good, out = offer(need, P._flag(argv, "--nozzle-material"))
    if "--json" in argv:
        print(json.dumps({"needs_grams": need, "offer": good, "excluded": out}, indent=1))
        return 0 if good else 1

    head = f"\n  {len(good)} spool(s) you can choose from"
    print(head + (f" for a {need:.0f} g job\n" if need else "\n"))
    for r in good:
        print(f"    ● {r['material']:6s} {r['colour'] or '':16.16s} {r['grams_left']:5d} g   "
              f"{r['words']}")
        for w in r.get("warnings") or []:
            print(f"        ⚠ {w}")
    if out:
        print(f"\n  not offered:\n")
        for r in out:
            print(f"    ○ {(r['material'] or '?'):6s} {r['colour'] or '':16.16s} — {r['excluded']}")
    if not good:
        print("  Nothing on the shelf can take this job. Load a spool, or shrink the part.\n")
    print()
    return 0 if good else 1


CMDS = {"list": cmd_list, "offer": cmd_offer}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    sys.exit(cli(CMDS[sys.argv[1]], sys.argv[2:]) or 0)
