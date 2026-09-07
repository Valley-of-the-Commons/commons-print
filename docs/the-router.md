# The router

Why this stack stops one step short of the printer, and why that is the design rather than
a gap in it.

## There is no login to build against

Bambu's Authorization Control System blocks third-party *writes* to a printer that is in
Cloud mode. An account login — the visitor's, the bench owner's, anyone's — buys telemetry
and nothing else. You cannot hand it a job. Control requires the printer to be in **LAN-only
Mode with Developer Mode on**, which is a decision made by a thumb on a touchscreen, not a
credential that can be delegated over the internet.

There is no OAuth, no scoped token, no partner API. So a login form in a printing interface
could only ever collect a password nobody has the right to hold, for an account that still
could not send the print.

Three things follow, and they are not preferences:

- **Nobody logs into Bambu through this.**
- **The printer is bound once, by its owner, at the machine.**
- **Visitors log into the bench, not into Bambu.**

## The router is the way through

There is one account that can already write to the machine, through software Bambu trusts
completely: **the owner's own, in the official app.** That is not a workaround, it is the
intended path — and it means the send problem was never a protocol problem.

So this stack sits *upstream* of Bambu Studio, not in place of it. It takes the file a
stranger brought, argues with it until it is plate-ready, and hands the owner a `.3mf` that
opens already correct. The owner looks at the verdict and presses print.

What that buys:

- **LAN-only Mode becomes optional.** The printer stays on Bambu Cloud, which keeps remote
  monitoring and the camera. For a machine other people queue on, being able to watch it
  from outside the room is not a luxury.
- **The release step exists for free.** A human between the queue and the nozzle is wanted
  anyway; the router is that human, with nothing extra to build.

The cost is that a print is never fully unattended. For a shared enclosed machine running
someone else's geometry, that was going to be true regardless.

## What "plate-ready" has to mean

The router's job is *look at the verdict, press print*. If the file needs fiddling before it
will go, the gates failed and the work landed back on a human who was supposed to be
supervising, not slicing. So the exported `.3mf` carries the machine profile, the plate, the
orientation gate 1b chose, the brim decision, and the filament mapped to the slot it is
actually in.

Which sets one rule about timing: **slice at release, not at submit.** A job that sat in a
queue while the spools were changed is a job whose slot mapping is now a lie, and whose
grams check is stale. Validate at submit so the visitor gets an answer; re-slice against the
live shelf the moment the router opens it.

## The router's hand is not a review

Custom start G-code is invisible in the slicer's interface. An owner pressing print cannot
see it and would not catch it. The strip-and-reslice rule at gate 0 is the entire reason the
router role is safe, and it gets stricter, not looser, once a trusted account is the one
sending.
