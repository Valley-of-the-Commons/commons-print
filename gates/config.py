"""
config.py — where the repo keeps the two things that differ between houses.

`printers.json` is the machine: bed, nozzle, whether it has a chamber. Committed, because
an X1C is an X1C wherever it stands.

`shelf.json` is the endpoint of whatever holds your spool inventory, and it is NOT
committed — it names a live service and a project. commons-spatial set that rule and it is
a good one: no keys, no endpoints, no project identifiers in a repo people are meant to
fork. `shelf.example.json` shows the shape.

Both can be overridden with COMMONS_PRINT_CONFIG for anyone running more than one bench.
"""
import json, os
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
