# Four traps in the OrcaSlicer CLI

All four found the expensive way against OrcaSlicer 2.4.2 on macOS. Three of them fail
loudly. The second one does not, which is why it is the one to read twice.

## 1. `--export-3mf` takes a bare filename

The CLI joins it onto `--outputdir` without checking, so an absolute path silently becomes
`<outputdir><abspath>`. The run slices correctly and then ends in `Unable to open the file`.

```
--export-3mf job.3mf --outputdir /some/where     ✅
--export-3mf /some/where/job.3mf                 🔴 slices, then cannot write
```

## 2. Presets must be flattened by you, marked `"from": "system"`

Three shapes, and only one is right:

| what you hand `--load-settings` | what happens |
|---|---|
| `"from": "User"`, flattened | `process not compatible with printer`, always — regardless of `compatible_printers`, `printer_settings_id`, or anything else you set |
| `"from": "User"`, thin with `inherits` | **passes the compatibility check and then resolves the inheritance wrong** |
| `"from": "system"`, flattened | correct |

The middle row is the dangerous one. Same mesh, same presets, same nozzle:

```
thin + inherits    4h 19m    filament_density 0
flattened + system 1h 13m    filament_density 1.26
```

It slices. It writes a G-code file. It reports a number that is wrong by a factor of three
and a half. Nothing in the output says so.

So resolve the inheritance chain yourself — `gates/profiles.py` does it in nine lines — and
hand the CLI finished configs. It never gets a chance to resolve anything.

## 3. `curr_bed_type` defaults to Cool Plate

Whatever machine you loaded. An X1C has textured PEI on it. Left alone, PLA gets a 35 °C bed
instead of 65, and the vendor's own plate-compatibility table — the thing `profiles.py`
exists to enforce, where a `0` means *this plate cannot be used for this material* — is
bypassed entirely. Set it explicitly on every run.

## 4. `--export-stls` produces nothing

Silently. Use `--export-stl`, singular. It lands in a `stl/` folder under `--outputdir`.

This one matters beyond the annoyance: `--export-stl` is how a project file gets converted
down to plain triangles at intake, which is what makes an incoming `.3mf` safe to print. The
plural spelling looks like the one you want when the file has several objects in it.

## Also worth knowing

- There is no OpenGL in a headless run, so thumbnail generation fails and says so at some
  length. Harmless — the plate renders when the file is opened.
- `Slicing volumes - removing top empty layers` is the normal end of a solid object, not a
  complaint about one. It matches most naive error greps.
- The G-code header carries `model printing time` and `total estimated time` on the *same
  line*. Anchoring a regex to the start of a comment finds the first and silently returns
  zero for the second.
- Material totals live in the footer. Read the head and the tail of the file, and make sure
  the two windows cannot leave a gap in the middle — a 300 KB G-code whose first 256 KB are
  read and whose end is only read past 512 KB reports no grams at all.
