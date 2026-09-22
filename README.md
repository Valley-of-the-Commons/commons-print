# commons-print

A gate stack between a person who has never printed anything and a shared 3D printer.
Seven small Python files, stdlib only, that take whatever somebody brings — an STL, a
project file, a MakerWorld link — and either hand back a plate-ready job or say, in words
that person can act on, why not.

Made for the makerspace at the Commons Hub, where a Bambu Lab printer with a four-slot
feeder stands in a room other people walk into. The problem it solves is not slicing. It is
that a model with no flat face, a spool that will eat the nozzle, and a profile cut for
somebody else's machine all look completely fine right up until the plate is wasted — and
the person who brought the file has no way to know that, because knowing it is the whole
craft.

`commons-spatial`'s SPEC §5 lists that printer among the machines "logged from photographs
and none of them verified by a human yet". This is the layer that would sit on top of it
once somebody has. Nothing here is Hub-specific: the machine is a config file.

```
workshop.py         all of it behind one command: run, doctor, selftest
gates/intake.py     0  · a link read for specs, a file converted down to triangles
gates/mesh.py       1a · watertight, walls, islands, fits the bed
gates/stance.py     1b · what touches the plate, where the weight lands
gates/slice.py      2+3· the real slice, and the truth read back out of the G-code
gates/profiles.py       the vendor's own filament rules, resolved
gates/shelf.py          the offer: on the shelf ∩ safe here ∩ enough grams left
config/                 your machine, and (uncommitted) your inventory endpoint
```

## Run it

Needs OrcaSlicer installed — it ships the vendor profile database, and its CLI does the
slicing. Nothing else.

```bash
python3 workshop.py doctor      # what is plugged in, in the order things block
python3 workshop.py selftest    # four shapes with known answers
python3 workshop.py run part.stl --filament "Bambu PLA Basic"
```

`doctor` says where it found the slicer. It looks in the macOS bundle, on `$PATH`
(`orca-slicer`, `OrcaSlicer`), in the places a Linux package lands, at an `Orca*.AppImage`
in `~/Applications`, `~/.local/bin` or `/opt`, and last at a flatpak — a flatpak is found
but only reaches the directories it was granted, so `flatpak override --user
--filesystem=<this repo>` may still be needed. Two environment variables end the search
when your install is somewhere else:

```bash
COMMONS_PRINT_ORCA           # the binary
COMMONS_PRINT_ORCA_PROFILES  # the vendor profile database, if it is not beside the binary
```

The second one is what an AppImage needs: it carries its `resources/profiles` inside the
image, where nothing on disk can read it until the image is mounted.

A run of a cone standing on its point:

```
 ✅  0 · intake    STL — a shape and nothing else
 ✅ 1a · geometry  watertight, 128 triangles, 50×50×60 mm — fits the bed
 ⚠  1b · stance    only 0.0 mm2 touches the plate — nothing for the first layer to hold
                   turned it over: 0% → 100% of its shadow on the plate (+100, not a gram spent)
 ✅  2 · slice     Bambu PLA Basic on the Textured PEI Plate, brim off
 ✅  3 · truth     0h 37m · 14.49 g · $0.36 · 298 layers to 60.0 mm
```

## The three ideas worth arguing with

**Turn it over before you reach for a raft.** Most parts that will not stick are not badly
shaped, they are lying on the wrong face because that is how the author left them in the
file. Rotating costs nothing. A brim costs a gram. A raft costs an hour and a scarred
bottom. Nervous software reaches for the raft first and pays the highest price for the
easiest problem, so `stance` reorients, measures again, and only then spends anything.

**Never print a file that arrived.** A `.3mf` is a zip, and a Bambu project file carries
`machine_start_gcode` inside it — arbitrary instructions that can drive the head or
override a temperature, invisible to anyone reviewing the plate. So intake keeps the
geometry and throws the container away: the file is converted to plain STL, which has
nowhere to put an instruction, and gate 2 re-slices from local profiles every time. Even
when the incoming file looks perfect. That is what makes "they choose the design, the bench
chooses the settings" true rather than aspirational.

Two kinds of container are not opened at all, because the slicer is the first thing to read
the file and it reads it whole: one carrying a plate somebody else already sliced, and one
carrying `post_process` — a list of shell commands the slicer knows how to run, written by
whoever made the file. Nothing is lost by refusing them. Gate 2 re-slices from local
profiles either way, so what was refused is a container that was going to be thrown away.

**Do not ask which filament.** The question only exists if you ask it. `shelf` intersects
three things nobody has to be troubled for — what is loaded right now, what the vendor's own
fields say is safe on this machine and nozzle, and whether enough grams remain for the job
gate 3 just measured — and offers what is left, described in words a person has. There is no
free-text filament field. You cannot select an abrasive against a soft nozzle, because it is
not on the list.

## What it does not do

**It does not send the job.** A printer in Bambu's Cloud mode refuses third-party writes
outright; control needs LAN-only plus Developer Mode, which is a thumb on a touchscreen, not
a credential anyone can delegate. So this stack stops at a plate-ready `.3mf` and a human
opens it in the official app. That human is the release step, and `docs/the-router.md` is
the argument for why that is the right place to stop rather than a limitation to route
around.

**It holds no credentials.** Not Bambu's, not MakerWorld's, not yours. A MakerWorld link's
specs are public and this reads them; the file is not, so it comes from your own account by
your own hand. `config/shelf.json` — your inventory endpoint — is gitignored, and its
passphrase comes from your environment or a keychain command, never from a file here.

**It has no interface yet.** This is the engine. The screens and the queue are not written.

## At the Hub specifically

Three joins to the rest of the house, none of them made:

- **The machines in §5 are unverified.** Somebody with a phone and an hour confirms what is
  actually on the shelf and in the feeder, and `config/printers.json` stops being a guess.
- **A job belongs to a room.** `commons-spatial` can pin a job onto the point where it lives
  in the building. A print queue is a list until it knows where the printer is standing.
- **The inventory is already a table.** `gates/shelf.py` wants a spool list with grams left;
  the Hub already runs boards and an inventory of two workshops. Pointing one at the other
  is a config file, not an integration.

## Where a hand would go furthest

- **Unsupported area.** Gate 3 reads time, grams, layers and height out of the G-code. It
  does not yet measure how much of the part is printing into thin air, which is the last
  number that decides whether supports were the right call.
- **Another machine.** Everything machine-specific is `config/printers.json` plus a name
  lookup in `gates/slice.py`. A Prusa or a Voron is a config file and a profile vendor, not
  a fork.
- **The screens.** Five of them, described in `docs/public-lane.md`. The interesting one is
  the third, where the stack says what it changed on somebody's behalf and why.
- **A queue.** Who brought it, what it costs, and who released it.

Read `docs/orca-cli.md` before touching gate 2. Four traps in that CLI cost an afternoon,
and the worst of them slices successfully and returns wrong numbers.
