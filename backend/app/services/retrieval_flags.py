"""retrieval_flags.py - one switch per retrieval experiment, all OFF by default.

Every experimental change on the `retrieval-experiments` branch sits behind one
flag here, so an A/B differs by exactly that flag and nothing else can drift
between the two arms.

THESE FLAGS ARE TEMPORARY. When a change is proved, its flag is deleted and the
change becomes unconditional; when a change loses, the flag and the code go
together. The handover diff contains no flag machinery - a permanent flag is a
permanent branch in the code, and the owner asked for simple.

--- STATUS 2026-08-30 (see RETRIEVAL-VERDICTS.md for the full writeup) ---

Two flags remain, and both are CUT, PARKED, DEFAULT OFF:

  - RETRIEVAL_CHUNK_IDENTITY (commit 88e294c) — stamping each chunk with its
    document/page. Held-out measured 23/24 -> 23/24 with reranking on: no change.
  - RETRIEVAL_SEAM_CHUNKS (commit 5c93696) — chunking on headings/page breaks
    instead of blind 500-word windows. Held-out measured 23/24 -> 23/24 with
    reranking on: no change.

Both act on candidate generation, and Cohere's reranker (a cross-encoder that
reads question+passage together) was already solving the problem they targeted.
They looked like wins ONLY when measured with reranking off — a different system
from the one that ships. Do not re-enable on that evidence.

REVISIT TRIGGER: re-run `9_eval.py retrieval --no-rerank` when the corpus passes
roughly 2,000 chunks. If recall@48 has fallen materially below 98%, candidate
generation has become the bottleneck (today's top-48 is 16% of the whole corpus;
at 1,000 documents it is a fraction of a percent) and these two switch on.

The table-router flag (RETRIEVAL_TABLE_ROUTER) that used to live here SHIPPED
2026-08-30 and is gone — the router in sql_tool.py now runs unconditionally.
"""
import os

_TRUE = {"1", "true", "yes", "on"}


def flag(name: str) -> bool:
    """Is experiment `name` on? Reads RETRIEVAL_<NAME>; anything unrecognised is OFF."""
    return os.environ.get(f"RETRIEVAL_{name.upper()}", "").strip().lower() in _TRUE
