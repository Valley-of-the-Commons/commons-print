"""
config.py — where the repo keeps the things that differ between houses.

`printers.json` is the machine: bed, nozzle, whether it has a chamber. Committed, because
an X1C is an X1C wherever it stands.

`shelf.json` is the endpoint of whatever holds your spool inventory, and it is NOT
committed — it names a live service and a project. commons-spatial set that rule and it is
a good one: no keys, no endpoints, no project identifiers in a repo people are meant to
fork. `shelf.example.json` shows the shape.

THE SLICER IS THE THIRD, and it is the one that differs most. OrcaSlicer is the only
requirement this stack has, but where it lands is a property of the machine rather than of
the bench: a `.app` bundle on macOS, a distribution package, an AppImage or a flatpak on
Linux. Written as one hardcoded path it was right on exactly one kind of computer, and
every other kind read "OrcaSlicer not found" while holding a working slicer — including
the Pi next to the printer that stdlib-only exists for. So `find_orca()` looks, in the
order below, and an environment variable ends the argument for anyone whose install is
somewhere else entirely.

Overrides, all of them environment variables, none of them required:
  COMMONS_PRINT_CONFIG          where printers.json and shelf.json live
  COMMONS_PRINT_RUNS            where jobs and their G-code land
  COMMONS_PRINT_ORCA            the slicer binary, when the search below misses it
  COMMONS_PRINT_ORCA_PROFILES   the vendor profile database, when it is not beside it
"""
import json, os, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = Path(os.environ.get("COMMONS_PRINT_CONFIG", ROOT / "config"))
RUNS = Path(os.environ.get("COMMONS_PRINT_RUNS", ROOT / "runs"))

PRINTERS = DIR / "printers.json"
SHELF = DIR / "shelf.json"


def printers():
    return json.loads(PRINTERS.read_text())


def active():
    p = printers()
    return p["active"], p["profiles"][p["active"]], p


def shelf_endpoint():
    if not SHELF.exists():
        return None
    return json.loads(SHELF.read_text())


# ---------- the slicer ----------

ORCA_ENV = "COMMONS_PRINT_ORCA"
ORCA_PROFILES_ENV = "COMMONS_PRINT_ORCA_PROFILES"

FLATPAK_ID = "io.github.softfever.OrcaSlicer"

ORCA_MISSING = (
    "OrcaSlicer not found. It ships the vendor profile database and does the slicing, so\n"
    "        every gate from 1b onwards needs it.\n"
    "          macOS   brew install --cask orcaslicer\n"
    "          Linux   the AppImage from github.com/SoftFever/OrcaSlicer/releases, or\n"
    "                  flatpak install flathub " + FLATPAK_ID + "\n"
    "        Installed somewhere this did not look? Set $" + ORCA_ENV + " to the binary.")

PROFILES_MISSING = (
    "OrcaSlicer's profile database not found — the vendor's own filament, machine and\n"
    "        process JSON, which is where every temperature and nozzle-hardness rule comes\n"
    "        from. It normally sits inside the install. An AppImage keeps it inside the\n"
    "        image, where it is not on disk until the image is mounted, so point at an\n"
    "        unpacked copy: set $" + ORCA_PROFILES_ENV + " to the directory holding BBL/.")


def _flatpak_files():
    """Where flatpak keeps an installed app's files. Read as a directory rather than asked
    of `flatpak info`, so resolving the slicer never spawns a process — `doctor` and every
    gate entry point call this, and a subprocess per call is a cost for nothing."""
    for base in (Path("/var/lib/flatpak"), Path.home() / ".local/share/flatpak"):
        p = base / "app" / FLATPAK_ID / "current/active/files"
        if p.is_dir():
            return p
    return None


def _appimages():
    """AppImages are files somebody downloaded, not packages, so they are wherever that
    person put them. These three are where they usually get put."""
    for d in (Path.home() / "Applications", Path.home() / ".local/bin", Path("/opt")):
        try:
            for f in sorted(d.glob("*.AppImage")):
                if f.name.lower().startswith("orca"):
                    yield f
        except OSError:
            continue


def find_orca():
    """(the argv prefix that runs the slicer, where it came from), or (None, None).

    A list rather than a path because a flatpak is run through `flatpak run` and has no
    binary a caller can hand to subprocess on its own."""
    env = os.environ.get(ORCA_ENV)
    if env:
        # Taken at its word, and not checked past. Somebody who sets this and gets it wrong
        # is owed "that path does not run" from the slicer, not a silent fall through to a
        # different install than the one they named.
        return [env], "$" + ORCA_ENV

    mac = Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
    if mac.exists():
        return [str(mac)], str(mac)

    for name in ("orca-slicer", "OrcaSlicer", "orcaslicer", "orca_slicer"):
        hit = shutil.which(name)
        if hit:
            return [hit], hit + "  (on $PATH)"

    for p in (Path("/usr/bin/orca-slicer"), Path("/usr/local/bin/orca-slicer"),
              Path("/opt/OrcaSlicer/orca-slicer"), Path("/opt/OrcaSlicer/bin/orca-slicer"),
              Path.home() / ".local/bin/orca-slicer"):
        if p.exists():
            return [str(p)], str(p)

    for img in _appimages():
        if os.access(img, os.X_OK):
            return [str(img)], str(img)

    fp = _flatpak_files()
    if fp:
        # Last on purpose, and it can be found and still not work: a flatpak sees only the
        # directories it was granted, and `runs/` is not one of them until somebody grants
        # it with `flatpak override --user --filesystem=<path>`. Reported anyway, so doctor
        # can name the install rather than claim there is none.
        return ["flatpak", "run", "--command=orca-slicer", FLATPAK_ID], "flatpak · " + FLATPAK_ID

    return None, None


def orca_command():
    """The argv prefix, or None. What a gate actually calls."""
    return find_orca()[0]


def orca_profiles():
    """The vendor profile database — `BBL/filament/*.json` and its neighbours — which
    profiles.py reads. Derived from wherever the binary turned up, because the two ship
    together and a database from one install describing another is a wrong answer rather
    than a missing one."""
    env = os.environ.get(ORCA_PROFILES_ENV)
    if env:
        return Path(env)

    cands = []
    cmd, _ = find_orca()
    if cmd and Path(cmd[0]).exists():
        b = Path(cmd[0]).resolve()
        cands += [b.parents[1] / "Resources" / "profiles",                        # .app bundle
                  b.parents[1] / "share" / "OrcaSlicer" / "resources" / "profiles",  # /usr/bin -> /usr/share
                  b.parent / "resources" / "profiles"]                            # unpacked beside it
    fp = _flatpak_files()
    if fp:
        cands.append(fp / "share" / "OrcaSlicer" / "resources" / "profiles")
    cands += [Path("/usr/share/OrcaSlicer/resources/profiles"),
              Path("/usr/local/share/OrcaSlicer/resources/profiles"),
              Path("/opt/OrcaSlicer/resources/profiles"),
              Path.home() / ".local/share/OrcaSlicer/resources/profiles"]

    for c in cands:
        if c.is_dir():
            return c
    return None
