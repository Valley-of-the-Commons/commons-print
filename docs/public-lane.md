# The public lane — someone else's design, our nozzle

How a person who does not own the machine gets a part printed without knowing what a raft is,
what a nozzle HRC is, or which of the 271 filament profiles applies to them.

Status: the gate stack runs. The screens and the queue are not written.

---

## 1. The fact that decides the whole shape

**There is no Bambu login to build against.**

Bambu's Authorization Control System blocks third-party *writes* to a printer that is
in Cloud mode. An account login — theirs or ours — buys telemetry and nothing else.
You cannot hand it a job. Control requires the printer to be in **LAN-only Mode with
Developer Mode on**, which is a decision made by a thumb on a touchscreen, not a
credential that can be delegated over the internet. `bambu.py setup` already says this.

Three things follow, and they are not preferences:

- **Nobody logs into Bambu through ClaudIA.** There is no OAuth, no scoped token, no
  partner API. The only thing a login form could collect is a password we have no right
  to hold, for an account that still could not send the print.
- **The printer is bound once, by its owner, at the machine.** IP + serial in
  `bambu.json`, access code in the vault. That binding is the entire trust relationship.
- **The visitor logs into ClaudIA, not Bambu.** Their identity is ours to issue.

So the question "how do people log into Bambu with the ClaudIA interface" has an answer,
and the answer is *they don't, and that is the safe version.*

### The router

There is one account that can already write to the machine, through software Bambu
trusts completely: **the owner's own, in the official app.** They are already logged in. That is
not a workaround, it is the intended path — and it means the send problem was never a
protocol problem.

**ClaudIA sits upstream of Bambu Studio, not in place of it.** She takes the file a
stranger brought, argues with it until it is plate-ready, and hands the owner a `.3mf`
that opens already correct. The owner looks at the verdict and presses print in the
official app.

What that buys, and it is more than convenience:

- **LAN-only Mode becomes optional.** The printer can stay on Bambu Cloud, which keeps
  Handy remote view, the camera away from the house, and cloud slicing intact. For a
  machine other people are queueing on, being able to watch it from outside the room is
  not a luxury.
- **Gate 4 leaves the critical path.** FTPS + MQTT is still `written, untested`, and it
  no longer blocks anything. It becomes a later optimisation for unattended release, not
  a prerequisite for the first stranger's part.
- **The release step exists for free.** Section 6 wanted a human between the queue and
  the nozzle. The router *is* that human, with no extra software to build.

The cost is that a print is never fully unattended: somebody has to press it. For a
shared enclosed machine that was going to be true anyway.

## 2. Two lanes, one gate stack

**Lane A — the house printer.** The visitor never touches Bambu at all. They bring a
design to the flow link, it clears the gates, it joins a queue, and the **router** — the
owner, logged into the official app — opens the job and presses print. No bridge, no LAN
requirement, no third-party write. Later, and only if the queue gets long enough to be
annoying, `bambu.py send` replaces the owner's hand for jobs already released.

**Lane B — their printer.** ClaudIA returns a sliced, plate-ready `.3mf` cut for *their*
machine and *their* spool, plus a verdict in plain language. They open it in their own
Bambu Studio or Handy and press print. The Bambu app is not skipped — it is the last
step, which is exactly where it belongs. No credential of theirs ever reaches us, and
nothing of ours reaches them.

**The two lanes produce the same artifact.** A gated, sliced, plate-ready `.3mf`. The
only difference is whose hand opens it — the router's, or the visitor's own. That is one
thing to build, not two, and it is why Lane B shipping to the public also finishes Lane A.

## 3. The gates

The four on the home page, with the two the public lane forces us to add.

| | gate | refuses | state |
|---|---|---|---|
| 0 | **intake** | wrong format, absurd size, a `.3mf` carrying its own G-code | ✅ `intake.py` |
| 1a | **geometry** | not watertight, flipped faces, walls under 2 nozzles, will not fit | ✅ `scad.py check` |
| 1b | **stance** | no base: too little bed contact, or tall enough to topple | ✅ `stance.py` |
| 2 | **slice** | the real X1C profile + the real spool, via the Orca CLI | ✅ `slice.py run` |
| 3 | **truth** | minutes, grams, layers and height read out of the G-code | ✅ `slice.py run` |
| 4 | **release** | the router opens it in the official app and presses print | to build (the queue) |
| 4b | **auto-send** | FTPS then MQTT — later, and only for already-released jobs | ◐ written, untested |

### Gate 0 — intake, and the one real attack

Accept `.stl`, `.step`, `.3mf`. Cap the upload, reject archives, reject anything with
more triangles than the box can chew.

The part that matters: **an uploaded `.3mf` can carry its own machine G-code.** Custom
start/end G-code rides inside the project file, and it is arbitrary — it can drive the
head, set temperatures past the profile, disable a sensor. A shared printer that prints
a stranger's `.3mf` as supplied is a shared printer that runs a stranger's instructions.

The rule is therefore absolute: **we never print an uploaded file. We print a file we
sliced.** Gate 2 re-slices from our own profiles every time, including when the visitor's
file looks perfect. That is the only version where "they choose the design, we choose the
machine settings" is actually true.

*How* the discard happens changed once it was built. The first attempt was zip surgery —
keep `3D/`, drop `Metadata/` — and it produced a container OrcaSlicer would not open at
all: a Bambu `.3mf` keeps its object transforms in `Metadata/model_settings.config`, so a
3mf with the metadata cut out is a 3mf with nothing placed on the plate. Reassembling one
by hand means owning a format Bambu changes at will.

The slicer's own loader does it better. `--export-stl` reads the project and writes the
geometry back out as plain STL — a format with nowhere to put an instruction: no settings,
no start G-code, no plate config, only triangles. The discard becomes total instead of
careful. (`--export-stls`, plural, silently produces nothing. Use the singular.)

### Gate 0 — the link

A MakerWorld link is the other way in, and the two halves of it are not equally open.

**The specs are public.** No account, no key: title, licence, and every print profile on
the design with the machine it was cut for, the nozzle, the filament type, the grams and
the seconds. `intake.py link` reads all of it.

**The file is not.** `Please log in to download models.` — MakerWorld's own words at a
403. So the file comes out of *their* account, by their hand, and arrives as an upload. We
hold no MakerWorld session any more than we hold a Bambu one. What the link buys is knowing
which of the profiles on that page is the right one *before* they go and fetch it.

And the profile is a hint, never a verdict. Three ways it goes wrong, all of them found in
a single real design on the first day of looking:

- **cut for another machine.** Primary compatibility A1 — open frame, no chamber — with
  the X1C only in the also-runs list.
- **the title disagrees with the file.** A profile called `PETG profile` whose
  machine-readable filament list says PLA. Somebody duplicated a profile, renamed it, and
  shipped it. `intake.py` flags the contradiction and says to trust the list.
- **the numbers are rounded to uselessness.** `weight: 1`, `usedG: 1` for a real part.

So the profile tells us what to expect and which file to ask for. The answer still comes
from gate 2 cutting it against this machine and this shelf.

The licence comes back too, and gets surfaced rather than enforced: none of the common ones
stop a person printing their own copy, but *non-commercial* and *exclusive* start to matter
the moment a hub charges for a print or passes the file on.

### Gate 1b — "no base"

This is the thing the question names first, and gate 1 does not currently catch it,
because a mesh can be flawless and still fall over.

Four numbers off the mesh, all cheap:

- **bed contact area** in the resting orientation — the footprint that actually touches
  the plate at z=0, not the bounding box.
- **contact ratio** — contact area against the projected silhouette. A cone on its point
  and a cone on its base are the same model; this is the number that tells them apart.
- **tipping** — centre of mass projected down, against the footprint polygon. Distance
  to the nearest edge, as a fraction of the footprint, is the margin.
- **slenderness** — height against the smaller footprint dimension.

The outcome is not a refusal, it is a *decision*, and the decision is where the interface
earns its keep:

```
contact ratio ≥ 0.6, margin healthy   → print as it lies, no brim
contact ratio 0.2 – 0.6               → brim, width from the ratio
contact ratio < 0.2, or tips          → try re-orienting first (Orca --orient auto)
still bad after re-orient             → raft, or supports, and say so in words
nothing works                         → refuse, with the reason a person can act on
```

Note that re-orienting is tried **before** reaching for a raft. A raft is what you use
when the part genuinely has no flat face; most "it won't stick" parts just landed on the
wrong one, and the slicer will find the right one for free.

### Gate 2 — the slice

OrcaSlicer 2.4.2 is on the machine with a full headless CLI. Everything needed exists:

```
OrcaSlicer --load-settings "<machine>.json;<process>.json" \
           --load-filaments "<filament>.json" \
           --orient auto --ensure-on-bed --arrange 1 \
           --slice 0 --export-3mf out.3mf --export-slicedata data/ \
           --outputdir <run>/ model.stl
```

The slicer's own refusals are part of the gate. It already knows about objects off the
bed, empty layers, a plate that cannot hold the temperature. We do not reimplement any
of that — we run it, read its stderr, and translate.

`--export-slicedata` is where gate 3 gets its numbers: real time, real grams per
filament, per plate. The model's estimate does not know about travel, supports, or a
brim it just added.

### The filament question answers itself

"They don't know the proper filament" is only a question if you ask it. So don't.

`profiles.py check` already resolves the vendor's own fields into a yes/no for this
machine and this nozzle — abrasion (`required_nozzle_HRC`), hotend ceiling, plate
compatibility (a zero means *not usable*), volumetric flow, enclosure, moisture. And
`fs_list` already knows which spools are on the shelf and how many grams are left.

Intersect the two and the chooser has nothing left to get wrong:

> **offer = on the shelf now  ∩  passes the check for this machine and nozzle  ∩  has
> more grams left than gate 3 says the job needs**

The visitor picks from a short list of things that are physically present and known-safe,
described in words they have ("tough and slightly flexible", "for something that lives
outside"), not in acronyms. There is no free-text filament field. There is no way to
select PA6-CF against a stainless nozzle, because it is not on the list.

Ordering matters: the grams check needs gate 3, so the flow is *offer the safe set →
slice against the chosen one → confirm it fits on the spool → then queue.*

## 4. What the visitor sees

Five screens. No jargon on any of them.

1. **Bring it** — drop a file, paste a MakerWorld or Printables link, or pick from the
   parts library. One thing at a time.
2. **What is it for** — decorative / functional / it lives outside / it touches food.
   Four taps that set strength, infill and the filament shortlist. This is the only
   question a non-printer can answer well, so it is the only one asked.
3. **The verdict** — the gates, in a row, each green or amber or red with one sentence.
   Amber says what ClaudIA changed on their behalf and why: *"laid it on its flat face
   instead of upright, and added a 5 mm brim — as it came, the base touched the plate
   over 4% of its width."* A red gate says what would have to change about the model.
4. **The cost** — 3 h 40, 62 g, from the G-code. Which spool it comes off. What is left
   after.
5. **Send** — Lane A: joins the queue for the router. Lane B: download the `.3mf`, with
   a QR to open it on the phone in Handy.

### What "plate-ready" has to mean

The router's job is *look at the verdict, press print*. If the file needs fiddling in
Bambu Studio before it will go, the gates failed and the work landed back on a human who
was supposed to be supervising, not slicing. So the exported `.3mf` carries, baked in:
the machine profile, the plate, the orientation gate 1b chose, the brim/raft/support
decision, and the filament mapped to **the AMS slot it is actually in** — read from the
shelf, not guessed.

Which sets one rule about timing: **slice at release, not at submit.** A job that sat in
the queue while the AMS was reloaded is a job whose slot mapping is now a lie, and the
grams check is stale too. Validate at submit so the visitor gets an answer; re-slice
against the live shelf the moment the router opens it.

The amber screen is the actual product. Anyone can refuse a bad file; the value is in
the ones that were fine all along and only needed to be turned over.

## 5. Build order

Each of these is useful before the next one exists.

1. ~~**Gate 2 as a script**~~ — `slice.py run` cuts a mesh against the real machine and
   the real spool and returns the plate-ready `.3mf`. Four CLI traps cost an afternoon and
   are written down in its docstring; the worst is that a thin preset with `inherits`
   *passes* the compatibility check and then resolves the inheritance wrong — same mesh,
   4h19m against the correct 1h13m, with `filament_density` 0. It slices, and it lies.
2. ~~**Gate 3**~~ — minutes, grams, layers and height parsed out of the G-code.
   Unsupported area is still to add.
3. ~~**Gate 1b**~~ — `stance.py check` measures, `stance.py fix` turns it over and measures
   again. A cone on its point goes from 0% contact to 100% without spending a gram on a
   raft. One measurement bug is worth remembering: the projected area of a closed mesh
   counts every point of its shadow twice, so a flat plate read 50% contact until the
   divisor was right.
4. ~~**Gate 0**~~ — `intake.py link` for the specs, `intake.py open` for the file.
5. **Lane B end to end** — the five screens, no accounts, no queue, no printer. Ships to
   anyone. Proves the gates in public before anything can touch the nozzle.
6. **Be your own first stranger.** Push a part through all five screens and print the
   `.3mf` it produces, untouched, in Bambu Studio. If it needed one adjustment, the gates
   are not done. Nobody else's file goes near a path we have not run ourselves.
7. **Identity + queue** — only now, and the queue is a router's page: what is waiting,
   what it costs, download and mark it printed. GreenPrint's door (email + 8-digit code)
   is already built and already handles bad wifi; use it rather than inventing one.
8. **Gate 4b, if ever** — FTPS + MQTT, tested on our own parts, and only to save the
   router a download on jobs they already released.

## 6. Standing refusals

- No Bambu credentials, ever — not a password field, not a stored token, not "just for
  telemetry". Section 1 is the reason.
- No stranger's LAN access code. If someone else wants their own printer driven, that is
  their own bridge on their own network, not our server holding their key.
- Nothing prints that we did not slice. Uploaded G-code is discarded at intake, always.
- No unattended release. Lane A queues; the router releases. An enclosed machine running
  someone else's geometry overnight with nobody in the house is a fire argument, not a
  software one.
- **The router's hand is not a review.** Custom start G-code is invisible in Bambu
  Studio's interface — the owner pressing print cannot see it and would not catch it. The
  strip-and-reslice rule at gate 0 is the entire reason the router role is safe, and it
  gets stricter, not looser, now that a trusted account is the one sending.
- The machine's owner can empty the queue and stop a job from anywhere in the flow, and
  that control is never gated behind the same login the visitors use.


