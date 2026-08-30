"""test_sql_tool_router_safety.py — acceptance tests for the pre-merge-review
fixes to the table-router wiring in sql_tool.py (doc-prep retrieval-steps
task 14, findings C3/I4/I5/I6).

C3 — `_TABLE_CARDS_PATH` used to be built with `parents[5]` to reach a
     doc-prep sibling folder OUTSIDE this repo. That (a) could IndexError at
     MODULE IMPORT on a shallow/container checkout layout — which no `try`
     inside `_load_table_cards` can catch, since it never runs — and (b) even
     when it didn't raise, pointed at a path that ships nowhere, so the
     router's token saving silently never happened in production. The fix:
     the cards now live inside this repo (backend/app/data/table_cards.json),
     resolved with two fixed `.parent` hops (never IndexErrors) and
     overridable via `TABLE_CARDS_PATH`.
I4 — `select_tables()` ran outside any try/except in `execute_sql_query`, so
     a validly-parsed-but-wrong-shaped cards file (e.g. a card missing its
     "table" key) raised KeyError/TypeError out of the request instead of
     degrading to the documented full-schema fallback.
I5 — `_fix_table_names` fuzzy-rewrote ANY SQL reference not in the routed
     subset onto the closest routed table name — including a reference that
     was already a real, existing table the router simply didn't select,
     silently substituting a different table's data into the answer.
I6 — `_table_cards_cache` never invalidated, so a newly-generated cards file
     (e.g. after a fresh doc-prep upload) was invisible until the process
     restarted.

Run:
    venv/Scripts/python -X utf8 tests/test_sql_tool_router_safety.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# C3a. The default cards path lives INSIDE this repo, not a doc-prep sibling,
#      and is built so it can never IndexError at import.
# ---------------------------------------------------------------------------
print("C3a. Default table-cards path is repo-internal and import-safe")

from app.services import sql_tool  # noqa: E402

check("default path lives under backend/app/data/table_cards.json",
      sql_tool._DEFAULT_TABLE_CARDS_PATH.parts[-3:] == ("app", "data", "table_cards.json"),
      str(sql_tool._DEFAULT_TABLE_CARDS_PATH))
check("default path does NOT reach outside this repo into a doc-prep sibling",
      "doc-prep" not in str(sql_tool._DEFAULT_TABLE_CARDS_PATH),
      str(sql_tool._DEFAULT_TABLE_CARDS_PATH))

# Reproduce the exact shape of the old bug on a deliberately shallow path
# (fewer than 6 parents) and prove the CURRENT construction (two fixed
# `.parent` hops) cannot raise IndexError the way `parents[5]` used to.
shallow = Path("C:/x/y.py") if os.name == "nt" else Path("/x/y.py")
raised = None
try:
    _ = shallow.resolve().parent.parent / "data" / "table_cards.json"
except IndexError as e:
    raised = e
check("two fixed .parent hops never IndexError, even on a very shallow path "
      "(the old parents[5] construction would have)", raised is None, repr(raised))

# ---------------------------------------------------------------------------
# C3b. The repo-internal file actually exists and actually loads non-zero
#      cards through the real (unpatched) _load_table_cards().
# ---------------------------------------------------------------------------
print("\nC3b. The committed cards file exists and loads for real")

check("backend/app/data/table_cards.json exists on disk",
      sql_tool._DEFAULT_TABLE_CARDS_PATH.exists(), str(sql_tool._DEFAULT_TABLE_CARDS_PATH))

sql_tool._table_cards_cache = None
sql_tool._table_cards_cache_mtime = None
real_cards = sql_tool._load_table_cards()
check("router loads a non-zero number of cards from the repo-internal file",
      len(real_cards) > 0, f"loaded {len(real_cards)} cards")
check("every loaded card has the shape select_tables() needs (a 'table' key)",
      all("table" in c for c in real_cards),
      [c for c in real_cards if "table" not in c][:1])

# ---------------------------------------------------------------------------
# C3c / I6. TABLE_CARDS_PATH overrides the path; the cache invalidates when
#           the (overridden) file's mtime changes; a corrupt or missing file
#           degrades to an empty list instead of raising.
# ---------------------------------------------------------------------------
print("\nC3c/I6. TABLE_CARDS_PATH override + mtime-based cache invalidation")

tmp_dir = Path(tempfile.mkdtemp(prefix="table_cards_test_"))
cards_path = tmp_dir / "table_cards.json"
cards_path.write_text(json.dumps([{"table": "t1"}]), encoding="utf-8")

_real_env = os.environ.get("TABLE_CARDS_PATH")
os.environ["TABLE_CARDS_PATH"] = str(cards_path)
importlib.reload(sql_tool)

check("TABLE_CARDS_PATH env var overrides the default path",
      sql_tool._TABLE_CARDS_PATH == cards_path, sql_tool._TABLE_CARDS_PATH)

first = sql_tool._load_table_cards()
check("cards loaded from the overridden path", first == [{"table": "t1"}], first)

# Rewrite the file with different content AND an explicitly different mtime
# (os.utime, not a sleep — makes the test independent of filesystem mtime
# granularity) to prove the cache notices without a process restart.
new_mtime = time.time() + 3600
cards_path.write_text(json.dumps([{"table": "t2"}]), encoding="utf-8")
os.utime(cards_path, (new_mtime, new_mtime))
second = sql_tool._load_table_cards()
check("cache invalidates when the file's mtime changes — a freshly "
      "regenerated cards file is picked up without a process restart (I6)",
      second == [{"table": "t2"}], second)

# Corrupt JSON degrades to [] rather than raising.
newer_mtime = new_mtime + 3600
cards_path.write_text("{not valid json", encoding="utf-8")
os.utime(cards_path, (newer_mtime, newer_mtime))
third = sql_tool._load_table_cards()
check("corrupt JSON degrades to an empty card list, not an exception", third == [], third)

# Missing file degrades to [] rather than raising.
os.environ["TABLE_CARDS_PATH"] = str(tmp_dir / "does_not_exist.json")
importlib.reload(sql_tool)
check("a missing file degrades to an empty card list", sql_tool._load_table_cards() == [])

# Restore the real environment/module state for the remaining tests.
if _real_env is None:
    os.environ.pop("TABLE_CARDS_PATH", None)
else:
    os.environ["TABLE_CARDS_PATH"] = _real_env
importlib.reload(sql_tool)


# ---------------------------------------------------------------------------
# I5. _fix_table_names must not rewrite a reference onto a different table
#     when that reference is already a REAL table for this user — only when
#     it genuinely doesn't exist anywhere should it fuzzy-match onto one of
#     the routed tables.
# ---------------------------------------------------------------------------
print("\nI5. _fix_table_names never silently swaps in a different real table")

from app.services.sql_tool import _fix_table_names  # noqa: E402

routed = ["hwu_panels", "hwu_smdb_feeders"]
all_live = ["hwu_panels", "hwu_smdb_feeders", "hwu_om_waterheaters_assets"]

# The router dropped hwu_om_waterheaters_assets, but the model correctly
# referenced it anyway (e.g. via a join-neighbour hint it saw elsewhere).
sql_correct_but_unrouted = 'SELECT * FROM "hwu_om_waterheaters_assets" WHERE qty > 0'
fixed = _fix_table_names(sql_correct_but_unrouted, routed, all_live)
check("a reference that genuinely exists for this user (just not routed) is "
      "left UNCHANGED, never silently rewritten onto a routed table",
      fixed == sql_correct_but_unrouted, fixed)

# A genuinely wrong/truncated name that exists NOWHERE for this user still
# gets fuzzy-fixed onto the closest routed table — the original behaviour
# this function exists for must survive.
sql_truncated = 'SELECT * FROM "hwu_pane" WHERE panel = \'MDB-C\''
fixed2 = _fix_table_names(sql_truncated, routed, all_live)
check("a name that matches NO real table anywhere is still fuzzy-fixed onto "
      "a routed table (regression: the original purpose of this function)",
      '"hwu_panels"' in fixed2, fixed2)

# Omitting all_table_names must reproduce the pre-fix behaviour exactly
# (defaults to real_table_names) — existing callers with no wider set still
# work unchanged.
fixed3 = _fix_table_names(sql_truncated, routed)
check("all_table_names defaults to real_table_names when omitted (backward compatible)",
      fixed3 == fixed2, fixed3)


# ---------------------------------------------------------------------------
# I4. A malformed cards file (valid JSON, wrong shape) must degrade to the
#     full-schema fallback inside execute_sql_query, never a raw exception.
# ---------------------------------------------------------------------------
print("\nI4. A malformed cards file degrades to the full schema, never a 500")

from app.services.table_router import select_tables  # noqa: E402

BAD_CARDS = [{"no_table_key_here": True}]

raised_directly = None
try:
    select_tables("anything", BAD_CARDS)
except Exception as e:
    raised_directly = e
check("select_tables() itself DOES raise on a malformed card (proves the "
      "premise: without a wrapper, this reaches execute_sql_query's caller)",
      isinstance(raised_directly, (KeyError, TypeError)), repr(raised_directly))


class _FakeExecResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def order(self, *a, **kw):
        return self

    def execute(self):
        return _FakeExecResult(self._data)


class _FakeSupabase:
    def table(self, name):
        if name == "structured_data":
            return _FakeQuery([
                {"table_name": "t1", "columns": ["x"], "rows": [{"x": 1}], "row_count": 1},
            ])
        return _FakeQuery([])


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None


class _FakeModels:
    def generate_content(self, *, model, contents, config=None):
        return _FakeResponse('SELECT x FROM "t1"')


class _FakeGenaiClient:
    def __init__(self, *a, **kw):
        self.models = _FakeModels()


_real_client_cls = sql_tool.genai.Client
_real_get_key = sql_tool.get_llm_api_key
_real_get_model = sql_tool.get_llm_model
_real_load_cards = sql_tool._load_table_cards

sql_tool.genai.Client = _FakeGenaiClient
sql_tool.get_llm_api_key = lambda: "fake-key"
sql_tool.get_llm_model = lambda: "fake-model"
sql_tool._load_table_cards = lambda: BAD_CARDS

raised = None
result_str = None
try:
    result_str = sql_tool.execute_sql_query("anything", "user-1", _FakeSupabase())
except Exception as e:
    raised = e
finally:
    sql_tool.genai.Client = _real_client_cls
    sql_tool.get_llm_api_key = _real_get_key
    sql_tool.get_llm_model = _real_get_model
    sql_tool._load_table_cards = _real_load_cards

check("execute_sql_query() does NOT crash when the cards file is malformed "
      "(I4: routing block is wrapped, falls back to the full schema)",
      raised is None, repr(raised))
check("the query still runs successfully via the full-schema fallback "
      "(not routed to zero tables)",
      raised is None and not (result_str or "").startswith("SQL query failed"),
      repr(result_str)[:200])


print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
