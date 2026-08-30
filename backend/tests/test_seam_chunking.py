"""test_seam_chunking.py - acceptance tests for Task 10 (seam-aware markdown
chunking, flag `seam_chunks`).

Plain-script style to match test_retrieval_flags.py / test_chunk_identity.py:
no pytest, no network, no Supabase. Run directly:

    "C:/RAG Automators/RAG Project/talk-to-the-building/backend/venv/Scripts/python.exe" \
        -X utf8 tests/test_seam_chunking.py

Fixtures are real bytes read from the actual doc-prep corpus, not invented
prose - `hwu_load_schedule_manifest.md` is the file the retrieval evidence
(cards lsh-005/006/007) was measured against, and the narrative/waterheaters
files supply real `<!-- Page N -->` markers and a real multi-row table.
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

from app.services import ingestion as ing
from app.services import retrieval_flags as rf

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond: FAILS.append(name)


def _clear_flag():
    os.environ.pop("RETRIEVAL_SEAM_CHUNKS", None)


DOC_PREP = Path(r"C:\RAG Automators\doc-prep\out\hwu-load-schedule")
MANIFEST_PATH = DOC_PREP / "hwu_load_schedule_manifest.md"
NARRATIVE_PATH = DOC_PREP / "hwu_load_schedule_narrative.md"
WATERHEATERS_PATH = Path(
    r"C:\RAG Automators\doc-prep\out\hwu-om-waterheaters-v2\hwu_om_waterheaters.md"
)

for p in (MANIFEST_PATH, NARRATIVE_PATH, WATERHEATERS_PATH):
    if not p.exists():
        print(f"FIXTURE MISSING: {p} - this test needs the real doc-prep corpus on disk")
        sys.exit(1)

MANIFEST_TEXT = MANIFEST_PATH.read_text(encoding="utf-8")

# Real excerpt spanning page 21 -> page 22 of the narrative (two consecutive
# <!-- Page N --> markers with real headings and real body text between them).
_narrative_lines = NARRATIVE_PATH.read_text(encoding="utf-8").split("\n")
PAGE_21_22_EXCERPT = "\n".join(_narrative_lines[159:213])
check("fixture sanity: page21/22 excerpt carries both real markers",
      "<!-- Page 21 -->" in PAGE_21_22_EXCERPT and "<!-- Page 22 -->" in PAGE_21_22_EXCERPT)

# Real 22-row markdown table (spares list) from the water heaters O&M manual.
_wh_lines = WATERHEATERS_PATH.read_text(encoding="utf-8").split("\n")
SPARES_TABLE_EXCERPT = "\n".join(_wh_lines[1415:1449])
check("fixture sanity: spares table excerpt carries the real header row",
      "| Item | Description | Part Number |" in SPARES_TABLE_EXCERPT)


# ── 1. splits happen at a page marker and at a heading ─────────────────────
print("\nseam splitting: page markers and headings")

chunks_pm = ing.chunk_markdown(PAGE_21_22_EXCERPT, chunk_size=120)
starts_at_marker = any(t.startswith("<!-- Page 22 -->") for t, _, _ in chunks_pm)
check("a chunk starts exactly at a <!-- Page N --> marker", starts_at_marker,
      [t[:40] for t, _, _ in chunks_pm])

chunks_manifest = ing.chunk_markdown(MANIFEST_TEXT)
starts_at_heading = any(t.startswith("## What these files contain") for t, _, _ in chunks_manifest)
check("a chunk starts exactly at a markdown heading", starts_at_heading,
      [t[:40] for t, _, _ in chunks_manifest])


# ── 2. a markdown table is never cut mid-row; header repeats on a split ────
print("\ntable handling: no mid-row cuts, header repeated on split")

original_rows = [l for l in SPARES_TABLE_EXCERPT.split("\n")
                  if l.startswith("| ") and "Item" not in l and "---" not in l]
check("fixture sanity: 22 real data rows in the spares table", len(original_rows) == 22,
      len(original_rows))

table_chunks = ing.chunk_markdown(SPARES_TABLE_EXCERPT, chunk_size=60)
check("the table did split (small budget forces it)", len(table_chunks) > 2, len(table_chunks))

recovered_rows = []
header_count = 0
for t, _, _ in table_chunks:
    lines = t.split("\n")
    if "| Item | Description | Part Number |" in lines:
        header_count += 1
    recovered_rows.extend(
        l for l in lines if l.startswith("| ") and "Item" not in l and "---" not in l
    )
check("no row was dropped or duplicated across the split",
      recovered_rows == original_rows,
      f"got {len(recovered_rows)} rows, want {len(original_rows)}")
check("the header row was repeated in more than one chunk (the split actually spans it)",
      header_count > 1, header_count)
check("every table-bearing chunk carries its own header row",
      all("| Item | Description | Part Number |" in t
          for t, _, _ in table_chunks if any(r in t for r in original_rows)))


# ── 3. the real regression: the board-hierarchy tree is not split mid-branch ─
print("\nthe lsh-005 regression case: the board-hierarchy tree")

old_chunks = ing.chunk_text(MANIFEST_TEXT, chunk_size=500, overlap=50)
old_hit = [c for c in old_chunks if "DB-06(B)-LAB-04" in c]
check("fixture sanity: old blind-window chunking reproduces the measured failure",
      len(old_hit) == 1 and len(old_hit[0]) > 3000,
      f"{len(old_hit)} hits, len={len(old_hit[0]) if old_hit else 'n/a'}")
old_chunk = old_hit[0]
old_pos_pct = 100 * old_chunk.find("DB-06(B)-LAB-04") / len(old_chunk)
print(f"    old: chunk len={len(old_chunk)}, DB-06(B)-LAB-04 at {old_pos_pct:.1f}%")

new_hit = [t for t, _, _ in chunks_manifest if "DB-06(B)-LAB-04" in t]
check("exactly one new chunk contains DB-06(B)-LAB-04", len(new_hit) == 1, len(new_hit))
new_chunk = new_hit[0]

tree_board_names = [
    "DB-06(B)-LAB-01", "DB-06(B)-LAB-02", "DB-06(B)-LAB-03",
    "DB-06(B)-LAB-04", "DB-06(B)-LAB-05", "DB-06(B)-WP-01",
]
check("the whole LAB branch of the tree stayed together in one chunk (not split mid-branch)",
      all(b in new_chunk for b in tree_board_names), new_chunk)
check("the tree's own fence markers are both present (never cut mid-fence)",
      new_chunk.count("```") == 2, new_chunk.count("```"))
check("the new chunk is materially smaller than the old 3,448-char monster chunk",
      len(new_chunk) < len(old_chunk) * 0.5,
      f"new={len(new_chunk)} old={len(old_chunk)}")
print(f"    new: chunk len={len(new_chunk)} (was {len(old_chunk)})")


# ── 4. page_start/page_end are correct across a page-marker boundary ───────
print("\npage_start/page_end tracking across a page-marker boundary")

separate = ing.chunk_markdown(PAGE_21_22_EXCERPT, chunk_size=400)
p21 = [c for c in separate if "SMDB-B-6F-LAB" not in c[0] and "SMDB NO. **SMDB-B-6F**" in c[0]]
p22 = [c for c in separate if "SMDB NO. **SMDB-B-6F-LAB**" in c[0]]
check("page 21 content lands in a chunk tagged page 21-21",
      len(p21) == 1 and p21[0][1] == 21 and p21[0][2] == 21, p21)
check("page 22 content lands in a chunk tagged page 22-22",
      len(p22) == 1 and p22[0][1] == 22 and p22[0][2] == 22, p22)

merged = ing.chunk_markdown(PAGE_21_22_EXCERPT, chunk_size=5000)
both_pages = [c for c in merged if "SMDB-B-6F**" in c[0] and "SMDB-B-6F-LAB**" in c[0]]
check("when both pages land in one chunk, its range spans 21-22",
      len(both_pages) == 1 and both_pages[0][1] == 21 and both_pages[0][2] == 22, both_pages)


# ── 5. flag OFF reproduces today's output byte-for-byte ────────────────────
print("\nflag OFF: chunk_document(.md) is byte-identical to today")

_clear_flag()
check("flag defaults OFF", rf.flag("seam_chunks") is False)

off_result = ing.chunk_document(MANIFEST_TEXT, "hwu_load_schedule_manifest.md")
expected_today = ing.chunk_text(MANIFEST_TEXT, chunk_size=500, overlap=50)
check("flag OFF: chunk_document output == chunk_text output, unchanged",
      off_result == expected_today,
      f"{len(off_result)} chunks vs {len(expected_today)} chunks")

# Non-.md files must never be touched by this flag either.
os.environ["RETRIEVAL_SEAM_CHUNKS"] = "1"
csv_text = "a,b\n1,2\n3,4\n"
csv_result = ing.chunk_document(csv_text, "x.csv")
check("flag ON: a .csv file is untouched (still routed through chunk_csv)",
      csv_result == ing.chunk_csv(csv_text), csv_result)

on_result = ing.chunk_document(MANIFEST_TEXT, "hwu_load_schedule_manifest.md")
check("flag ON: chunk_document(.md) output differs from the old word-window baseline",
      on_result != expected_today)
check("flag ON: chunk_document(.md) output equals chunk_markdown's text (page info stripped)",
      on_result == [t for t, _, _ in ing.chunk_markdown(MANIFEST_TEXT, "hwu_load_schedule_manifest.md")])

_clear_flag()


# ── extra: chunk_markdown itself falls back to the word splitter on plain prose ─
print("\nchunk_markdown(): falls back to the word-window splitter on seamless prose")

plain_prose = "This is just a narrative paragraph with no headings, tables, fences, " \
              "page markers or trees at all. " * 40
fallback_result = ing.chunk_markdown(plain_prose, chunk_size=120)
expected_fallback = [(c, None, None) for c in ing.chunk_text(plain_prose, chunk_size=120, overlap=50)]
check("no discoverable seam -> falls back to chunk_text, wrapped as (text, None, None)",
      fallback_result == expected_fallback,
      f"{len(fallback_result)} vs {len(expected_fallback)}")


print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
