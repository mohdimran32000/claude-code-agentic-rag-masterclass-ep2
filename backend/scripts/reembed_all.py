"""Re-embed every chunk after migration 022 (768 -> 1536 dims + task_type).

Migration 022 drops and re-adds document_chunks.embedding, so every embedding is
NULL when it finishes. `content` and `tsv` survive, so keyword search keeps working
in the meantime — but vector search returns nothing until this has run.

Reads chunk text straight from the database (no need to re-read source files) and
fills the embedding column back in with TASK_TYPE_DOCUMENT.

Usage:
    cd backend && venv/Scripts/python scripts/reembed_all.py [--user <uuid-prefix>]
    Add --dry-run to see the work without calling the API.
"""
import argparse
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from supabase import create_client  # noqa: E402

from app.services.ingestion import EMBEDDING_DIMS, TASK_TYPE_DOCUMENT, embed_batch  # noqa: E402

BATCH = 50


def page_all(sb, table, cols, **eq):
    out, start, step = [], 0, 1000
    while True:
        q = sb.table(table).select(cols)
        for k, v in eq.items():
            q = q.eq(k, v)
        rows = q.range(start, start + step - 1).execute().data or []
        out.extend(rows)
        if len(rows) < step:
            return out
        start += step


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None, help="only this user id (or prefix)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    chunks = page_all(sb, "document_chunks", "id,user_id,content,document_id")
    if args.user:
        chunks = [c for c in chunks if str(c["user_id"]).startswith(args.user)]

    missing = [c for c in chunks if c.get("content")]
    print(f"chunks to embed : {len(missing)}")
    print(f"target dims     : {EMBEDDING_DIMS}")
    print(f"task type       : {TASK_TYPE_DOCUMENT}")
    if args.dry_run:
        print("\n--dry-run, nothing sent")
        return 0

    done, t0 = 0, time.time()
    for i in range(0, len(missing), BATCH):
        batch = missing[i:i + BATCH]
        vectors = embed_batch([c["content"] for c in batch])
        if len(vectors) != len(batch):
            print(f"FAIL: got {len(vectors)} vectors for {len(batch)} chunks")
            return 1
        for c, v in zip(batch, vectors):
            if len(v) != EMBEDDING_DIMS:
                print(f"FAIL: chunk {c['id']} got {len(v)} dims, expected {EMBEDDING_DIMS}")
                return 1
            sb.table("document_chunks").update({"embedding": v}).eq("id", c["id"]).execute()
        done += len(batch)
        print(f"  {done}/{len(missing)}  ({time.time() - t0:.0f}s)")

    still_null = [c for c in page_all(sb, "document_chunks", "id,embedding")
                  if c.get("embedding") is None]
    print(f"\nre-embedded {done} chunk(s) in {time.time() - t0:.0f}s")
    print(f"chunks still without an embedding: {len(still_null)}")
    return 1 if still_null else 0


if __name__ == "__main__":
    sys.exit(main())
