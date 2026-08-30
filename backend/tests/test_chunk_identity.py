"""test_chunk_identity.py - acceptance tests for Task 7 (chunk identity).

Plain-script style to match test_retrieval_flags.py / test_reranker_scores.py:
no pytest, no network, no Supabase. Run directly:

    "C:/RAG Automators/RAG Project/talk-to-the-building/backend/venv/Scripts/python.exe" \
        -X utf8 tests/test_chunk_identity.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import must succeed with no real Gemini key — ingestion.py builds its client
# at import time from GEMINI_API_KEY, but genai.Client() must not touch the
# network just to be constructed. Poisoning the env var here proves this test
# doesn't accidentally depend on a real key being present.
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

from app.services import ingestion as ing
from app.services import retrieval_flags as rf

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond: FAILS.append(name)


def _clear_flag():
    os.environ.pop("RETRIEVAL_CHUNK_IDENTITY", None)


# ── identity_header: purity, shape, length, omission ──────────────────────
print("identity_header()")

_clear_flag()

h1 = ing.identity_header(
    doc_title="Om Firefighting", file_name="hwu_om_firefighting_asset_register.csv",
    page_start=46, page_end=46,
)
h2 = ing.identity_header(
    doc_title="Om Firefighting", file_name="hwu_om_firefighting_asset_register.csv",
    page_start=46, page_end=46,
)
check("pure: identical inputs -> identical string", h1 == h2, f"{h1!r} != {h2!r}")

check("real example: contains the file name", "hwu_om_firefighting_asset_register.csv" in h1, h1)
check("real example: contains the page", "p46" in h1, h1)
check("real example: stays well under 120 chars", len(h1) <= 120, f"len={len(h1)} {h1!r}")
check("real example: shaped like [ ... ]", h1.startswith("[") and h1.endswith("]"), h1)
print(f"    example: {h1}")

h_range = ing.identity_header(doc_title="Om Firefighting", file_name="x.csv", page_start=32, page_end=40)
check("a page range renders p<start>-<end>", "p32-40" in h_range, h_range)

h_tags = ing.identity_header(file_name="x.csv", tags=["fire", "sprinkler"])
check("tags render as 'tags: a, b'", "tags: fire, sprinkler" in h_tags, h_tags)

# Omission: no field should ever leave a bare " · " artifact or the word "None".
h_min = ing.identity_header(file_name="x.csv")
check("unknown fields omitted, not emitted empty", h_min == "[x.csv]", h_min)
check("never leaks the literal 'None'", "None" not in h_min, h_min)

h_empty = ing.identity_header()
check("all-unknown input never crashes", h_empty == "[]", h_empty)

# Defensive length cap under a pathological input.
h_long = ing.identity_header(
    doc_title="X" * 200, file_name="Y" * 200, section="Z" * 200,
    page_start=1, page_end=2, tags=["a" * 50, "b" * 50],
)
check("hard cap holds even for a pathological input", len(h_long) <= 120, f"len={len(h_long)}")

# ── the guard that matters most: same body, different file -> different string ──
print("\nidentity distinguishes look-alikes")

h_docA = ing.identity_header(doc_title="Water Heaters", file_name="hwu_om_waterheaters_asset_register.csv", page_start=10, page_end=10)
h_docB = ing.identity_header(doc_title="Fire Fighting", file_name="hwu_om_firefighting_asset_register.csv", page_start=10, page_end=10)
check("different doc_title/file_name -> different header", h_docA != h_docB, f"{h_docA!r} == {h_docB!r}")


# ── _csv_chunk_page_range: pure, CSV-shaped, tolerant of non-CSV/no-page-column ──
print("\n_csv_chunk_page_range()")

csv_chunk = "source_page,a,b\n5,x,y\n7,x,y\n6,x,y\n"
check("reads min/max from a source_page column", ing._csv_chunk_page_range(csv_chunk) == (5, 7),
      str(ing._csv_chunk_page_range(csv_chunk)))

no_page_col = "a,b\n1,2\n3,4\n"
check("no page-shaped column -> (None, None)", ing._csv_chunk_page_range(no_page_col) == (None, None))

prose = "This is just a narrative paragraph, not a table at all."
check("non-CSV body text -> (None, None), no crash", ing._csv_chunk_page_range(prose) == (None, None))

check("pure: same chunk text -> same range twice",
      ing._csv_chunk_page_range(csv_chunk) == ing._csv_chunk_page_range(csv_chunk))


# ── title= only ever reaches the API for RETRIEVAL_DOCUMENT ────────────────
print("\nembed_text()/embed_batch(): title= plumbing (no network)")

captured = []
class _FakeEmbedding:
    def __init__(self):
        self.values = [0.0]
class _FakeResponse:
    def __init__(self, n):
        self.embeddings = [_FakeEmbedding() for _ in range(n)]

def _fake_embed_with_retry(func, *args, **kwargs):
    captured.append(dict(kwargs.get("config") or {}))
    contents = kwargs.get("contents")
    n = len(contents) if isinstance(contents, list) else 1
    return _FakeResponse(n)

ing._embed_with_retry = _fake_embed_with_retry

captured.clear()
ing.embed_text("hello")
check("no title given -> config carries no 'title' key", "title" not in captured[-1], captured[-1])

captured.clear()
ing.embed_text("hello", title="Fire Fighting")
check("title given + DOCUMENT task -> config carries it", captured[-1].get("title") == "Fire Fighting", captured[-1])

captured.clear()
ing.embed_text("hello", task_type=ing.TASK_TYPE_QUERY, title="Fire Fighting")
check("title given but QUERY task -> dropped, never sent", "title" not in captured[-1], captured[-1])

captured.clear()
ing.embed_batch(["a", "b"])
check("embed_batch with no title -> config carries no 'title' key", "title" not in captured[-1], captured[-1])

captured.clear()
ing.embed_batch(["a", "b"], title="Fire Fighting")
check("embed_batch with a title -> one title per batch call", captured[-1].get("title") == "Fire Fighting", captured[-1])


# ── the flag-off guard: byte-identical to today's output ───────────────────
print("\n_embed_and_build_chunk_rows(): flag OFF reproduces today's output")

_clear_flag()
check("flag defaults OFF", rf.flag("chunk_identity") is False)

embed_calls = []
def _fake_embed_batch(texts, batch_size=50, title=None):
    embed_calls.append({"texts": list(texts), "title": title})
    return [[float(i)] for i in range(len(texts))]

_real_embed_batch = ing.embed_batch
ing.embed_batch = _fake_embed_batch

chunks = ["chunk one body text", "chunk two body text"]
embed_calls.clear()
rows_off = ing._embed_and_build_chunk_rows(chunks, "doc-1", "user-1", "hwu_x.md", "/om-firefighting")

# This is literally the pre-chunk_identity code from ingest_document/_update.
expected_off = [
    {
        "document_id": "doc-1",
        "user_id": "user-1",
        "content": chunk,
        "embedding": embedding,
        "chunk_index": idx,
        "content_hash": ing.compute_chunk_hash(chunk),
    }
    for idx, (chunk, embedding) in enumerate(zip(chunks, [[0.0], [1.0]]))
]

check("flag OFF: rows are byte-identical to the pre-change shape", rows_off == expected_off,
      f"\n  got: {rows_off}\n  want: {expected_off}")
check("flag OFF: embed_batch received the RAW chunks, untouched",
      embed_calls == [{"texts": chunks, "title": None}], embed_calls)
check("flag OFF: no identity columns leak onto the row at all",
      all("doc_title" not in r and "file_name" not in r and "folder_path" not in r for r in rows_off),
      rows_off)

# ── flag ON: header is prepended, title is passed, identity columns are set ─
print("\n_embed_and_build_chunk_rows(): flag ON stamps content + columns")

os.environ["RETRIEVAL_CHUNK_IDENTITY"] = "1"
check("flag now ON", rf.flag("chunk_identity") is True)

embed_calls.clear()
rows_on = ing._embed_and_build_chunk_rows(chunks, "doc-1", "user-1",
                                           "hwu_om_firefighting_asset_register.csv", "/om-firefighting")

check("flag ON: content is stamped (longer than the raw chunk)",
      all(len(rows_on[i]["content"]) > len(chunks[i]) for i in range(len(chunks))))
check("flag ON: content starts with an identity header",
      all(rows_on[i]["content"].startswith("[") for i in range(len(chunks))))
check("flag ON: raw chunk body still present verbatim after the header",
      all(rows_on[i]["content"].endswith(chunks[i]) for i in range(len(chunks))))
check("flag ON: identity columns populated",
      all(r["file_name"] == "hwu_om_firefighting_asset_register.csv" for r in rows_on) and
      all(r["folder_path"] == "/om-firefighting" for r in rows_on) and
      all(r["doc_title"] == "Om Firefighting" for r in rows_on),
      rows_on)
check("flag ON: embed_batch got a title for this document",
      embed_calls[0]["title"] == "Om Firefighting", embed_calls)
check("flag ON: embed_batch received the STAMPED texts, not the raw chunks",
      embed_calls[0]["texts"] == [r["content"] for r in rows_on], embed_calls)

# The guard that matters most, at the full-row level: identical body text,
# different source file -> different embedded/stored string (and therefore no
# more accidental collision in openai_client.py's content-string back-map).
embed_calls.clear()
rows_docA = ing._embed_and_build_chunk_rows(
    ["identical body text"], "doc-A", "user-1", "hwu_om_waterheaters_asset_register.csv", "/om-waterheaters")
rows_docB = ing._embed_and_build_chunk_rows(
    ["identical body text"], "doc-B", "user-1", "hwu_om_firefighting_asset_register.csv", "/om-firefighting")
check("identical body, different file -> different stored content",
      rows_docA[0]["content"] != rows_docB[0]["content"],
      f"{rows_docA[0]['content']!r} == {rows_docB[0]['content']!r}")

# restore
ing.embed_batch = _real_embed_batch
_clear_flag()

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
