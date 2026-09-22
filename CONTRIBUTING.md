# Contributing

```bash
python3 workshop.py doctor      # OrcaSlicer is the only requirement
python3 workshop.py selftest    # four shapes with known answers, ~40 seconds
```

`doctor` names the path it found the slicer at. Set `COMMONS_PRINT_ORCA` (the binary) or
`COMMONS_PRINT_ORCA_PROFILES` (the vendor profile database) if yours is somewhere the
search in `gates/config.py` does not look. No gate hardcodes a slicer path.

No build step, no package manager, no dependencies. Every gate is a plain module that also
runs on its own:

```bash
python3 -m gates.stance check part.stl
python3 -m gates.slice  run   part.stl --filament "Bambu PLA Basic"
python3 -m gates.intake link  https://makerworld.com/en/models/…
```

## Add a case to the selftest before you add a gate

`workshop.py selftest` builds four shapes in code — a lidless box, a cone on its point, a
12:1 tower, a flat plate — and asserts *which gate fires* on each. It exists so a gate that
quietly stops refusing shows up in a terminal rather than on somebody's plate. A new rule
that nothing can prove is a new rule nobody can trust six months from now.

## Where a hand would go furthest

Read the README's last section. Briefly: unsupported area in gate 3, a second machine
family, the five screens, and a queue.

## What belongs somewhere else

- **Dependencies.** Stdlib only is what makes this droppable onto a Raspberry Pi next to the
  printer. If a gate needs numpy, that gate probably needs less ambition.
- **Anything reaching a live service.** No keys, no endpoints, no project identifiers, so
  this stays safe for anyone to fork. `config/shelf.json` is gitignored for exactly this
  reason and `config/shelf.example.json` is the shape it takes.
- **The send.** Driving a printer over MQTT belongs in whatever runs on the bench's own
  network, not in the repo people fork. `docs/the-router.md` is the argument.

## Style

Comments explain *why*, and especially why something is not the obvious thing — the
turn-before-brim-before-raft order, the reason an incoming file is never printed, the reason
inheritance is resolved here rather than by the slicer. A comment that restates the line
above it is worse than none. Findings that cost an afternoon go in `docs/orca-cli.md` so
they cost it once.

A library function says no by raising a `Refusal` (`gates/refusal.py`), never by calling
`sys.exit`. The exit belongs to `__main__`, where `refusal.cli` turns it back into the same
sentence on stderr and the same exit code. `usage:` lines stay `sys.exit` — being called
with the wrong arguments is a fact about a command line. The reason is the screens: a
request handler that calls a gate and gets `sys.exit` dies with the response half-written.

Numbers in prose are measured, not estimated. "4h19m against the correct 1h13m" is worth
writing down; "much slower" is not.
