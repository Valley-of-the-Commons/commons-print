"""
refusal.py — a stop with a type on it, so something other than a terminal can report one.

WHY THIS IS NOT JUST sys.exit. Half of a gate's refusals are written as `sys.exit("[shelf]
no shelf configured…")` inside a function any caller may import. In a terminal that is
exactly right: the sentence lands on stderr, the shell gets 1, and nothing has to be
arranged. Anywhere else it is a process kill with a string attached. workshop.py already
has to wrap two `shelf.offer()` calls in `except SystemExit` to survive its own library,
and the screens in docs/public-lane.md cannot survive it at all — a request handler that
calls `shelf.offer()` and gets `sys.exit` dies with the response half-written, and the
visitor sees a 500 where a sentence about a spool belongs.

So the sentence becomes a value that can be carried somewhere else and still be the same
sentence:

  code       what happened, in something a caller can branch on without matching English
  sentence   the line a person reads — the one sys.exit already printed, unchanged
  fix        the follow-on lines, when there is something to do about it

It is an Exception because raising is how a refusal already travels out of six frames of
library code, and nothing between here and the caller has to learn about it. `text()`
rebuilds what sys.exit printed, gutter and all, so a terminal reads exactly what it read
before — that is the whole compatibility claim of this change, and the selftest pins it.

Codes are dotted and lowercase: `shelf.unconfigured`, `stance.not_a_mesh`. The prefix is
the gate, which is what lets a caller tell a refusal somebody can answer — load a spool,
name a filament — from one nobody can, like a file that is not a mesh.

What is NOT a Refusal: `usage:` lines. Being called with the wrong arguments is a fact
about a command line and belongs to the command line, so those stay `sys.exit`.
"""
import sys


class Refusal(Exception):
    """A gate saying no, in a form that survives leaving the process it was said in."""

    # What sys.exit's follow-on lines were indented by. Kept exactly, because the point of
    # this type is that the terminal cannot tell the difference.
    GUTTER = " " * 8

    def __init__(self, code, sentence, fix=None):
        self.code = code
        self.sentence = sentence
        self.fix = fix
        super().__init__(self.text())

    def text(self):
        lines = [self.sentence]
        for line in (self.fix or "").splitlines():
            lines.append(self.GUTTER + line)
        return "\n".join(lines)

    def as_dict(self):
        """What a queue or a screen stores: the code to act on, the sentence to show."""
        return {"code": self.code, "sentence": self.sentence, "fix": self.fix}


def cli(fn, argv):
    """The command-line half, and the only place a Refusal is allowed to end a process.
    Same text on stderr, same exit code 1, as the sys.exit it replaced."""
    try:
        return fn(argv)
    except Refusal as r:
        sys.exit(r.text())
