"""retrieval_flags.py - one switch per retrieval experiment, all OFF by default.

Every experimental change on the `retrieval-experiments` branch sits behind one
flag here, so an A/B differs by exactly that flag and nothing else can drift
between the two arms.

THESE FLAGS ARE TEMPORARY. When a change is proved, its flag is deleted and the
change becomes unconditional; when a change loses, the flag and the code go
together. The handover diff contains no flag machinery - a permanent flag is a
permanent branch in the code, and the owner asked for simple.
"""
import os

_TRUE = {"1", "true", "yes", "on"}


def flag(name: str) -> bool:
    """Is experiment `name` on? Reads RETRIEVAL_<NAME>; anything unrecognised is OFF."""
    return os.environ.get(f"RETRIEVAL_{name.upper()}", "").strip().lower() in _TRUE
