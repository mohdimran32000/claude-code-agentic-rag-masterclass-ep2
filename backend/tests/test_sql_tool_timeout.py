"""test_sql_tool_timeout.py — acceptance tests for the SQL_QUERY_TIMEOUT fix
(doc-prep retrieval-steps task 12, defect 1).

BEFORE this fix: SQL_QUERY_TIMEOUT was declared in sql_tool.py and referenced
nowhere else — DuckDB ran model-generated SQL with no time limit at all.
A cartesian join (or any runaway query) would block the request forever.
Test 3 below reproduces exactly that shape of query through the real,
undoctored execute_sql_query() entry point: without the fix it never
returns (this script would hang until something outside it gives up); with
the fix it returns within a few seconds of SQL_QUERY_TIMEOUT.

The slowness in every test below is real DuckDB work (a huge cross join via
range()), never a Python time.sleep() standing in for it — the point is to
prove DuckDB itself gets interrupted mid-query, not that a timer fires.

Run:
    venv/Scripts/python -X utf8 tests/test_sql_tool_timeout.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

from app.services import sql_tool
from app.services.sql_tool import (
    SQL_QUERY_TIMEOUT,
    _execute_with_timeout,
    _QueryTimeoutError,
    execute_sql_query,
)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond:
        FAILS.append(name)


# A query that is genuinely expensive for DuckDB to run to completion (a
# ~10^16-row cross join) but costs nothing to START — the timeout has to
# interrupt real, in-progress work, not something that would finish on its
# own inside the test's patience.
SLOW_SQL = "SELECT count(*) FROM range(100000000) a, range(100000000) b"


# ---------------------------------------------------------------------------
# 1. The mechanism itself: DuckDB is actually interrupted, within the given
#    timeout, and nothing is left broken or leaked afterwards.
# ---------------------------------------------------------------------------
print("1. _execute_with_timeout() interrupts a genuinely slow DuckDB query")

con = duckdb.connect(":memory:")
threads_before = threading.active_count()

t0 = time.time()
raised = None
try:
    _execute_with_timeout(con, SLOW_SQL, timeout=0.5)
except _QueryTimeoutError as e:
    raised = e
elapsed = time.time() - t0

check("raises _QueryTimeoutError", raised is not None)
check("aborts close to the timeout, not instantly and not never",
      raised is not None and 0.4 <= elapsed <= 5.0, f"elapsed={elapsed:.2f}s")
check("exception message names the timeout", raised is not None and "0.5s" in str(raised), str(raised))

time.sleep(0.1)  # give threading.Timer's thread a beat to fully exit
check("no thread leaked behind", threading.active_count() <= threads_before,
      f"before={threads_before} after={threading.active_count()}")

check("connection still usable after an interrupt",
      con.execute("SELECT 1").fetchall() == [(1,)])

try:
    con.close()
    closed_ok = True
except Exception as e:
    closed_ok = False
    print(f"    con.close() raised: {e}")
check("connection closes cleanly after an interrupt", closed_ok)


# ---------------------------------------------------------------------------
# 2. Regression: an ordinary fast query is completely unaffected.
# ---------------------------------------------------------------------------
print("\n2. A normal fast query still works, unaffected by the timeout machinery")

con2 = duckdb.connect(":memory:")
result = _execute_with_timeout(con2, "SELECT 1, 2, 3", timeout=SQL_QUERY_TIMEOUT)
check("fast query returns its real result", result == [(1, 2, 3)], result)
con2.close()


# ---------------------------------------------------------------------------
# 3. End-to-end through the real, undoctored execute_sql_query(): the model
#    "generates" the slow cross join, and the function must still return —
#    with the SAME failure-string shape openai_client.py matches on to
#    trigger its document-search fallback ("SQL query failed: " prefix,
#    openai_client.py:1277). Only the LLM/network edges (Supabase table
#    fetch, Gemini SQL generation) are faked; everything from SQL generation
#    onward — DuckDB table load, execute, timeout, error formatting — is the
#    real code path.
#
# This test uses the REAL production SQL_QUERY_TIMEOUT (no shrinking it for
# the test) specifically so it also proves the shipped constant, not a
# stand-in value, actually governs the abort.
# ---------------------------------------------------------------------------
print(f"\n3. execute_sql_query() end-to-end: a runaway query still returns "
      f"'SQL query failed: ...' within ~{SQL_QUERY_TIMEOUT}s")


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
    """Only structured_data is ever queried by execute_sql_query."""

    def table(self, name):
        if name == "structured_data":
            return _FakeQuery([
                {"table_name": "t1", "columns": ["x"], "rows": [{"x": 1}], "row_count": 1},
            ])
        return _FakeQuery([])


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None  # llm_usage._usage_dict handles this gracefully


class _FakeModels:
    def generate_content(self, *, model, contents, config=None):
        return _FakeResponse(SLOW_SQL)


class _FakeGenaiClient:
    def __init__(self, *a, **kw):
        self.models = _FakeModels()


# Patch the network/DB edges only. genai is a shared module object (`from
# google import genai`), so restore it afterward regardless of outcome.
_real_client_cls = sql_tool.genai.Client
_real_get_key = sql_tool.get_llm_api_key
_real_get_model = sql_tool.get_llm_model
_real_load_cards = sql_tool._load_table_cards

sql_tool.genai.Client = _FakeGenaiClient
sql_tool.get_llm_api_key = lambda: "fake-key"
sql_tool.get_llm_model = lambda: "fake-model"
sql_tool._load_table_cards = lambda: []  # no router narrowing — deterministic regardless of doc-prep checkout

try:
    t0 = time.time()
    result_str = execute_sql_query("does not matter — SQL is faked", "user-1", _FakeSupabase())
    elapsed = time.time() - t0
finally:
    sql_tool.genai.Client = _real_client_cls
    sql_tool.get_llm_api_key = _real_get_key
    sql_tool.get_llm_model = _real_get_model
    sql_tool._load_table_cards = _real_load_cards

check("returns within a few seconds of SQL_QUERY_TIMEOUT (not left hanging)",
      elapsed < SQL_QUERY_TIMEOUT + 10, f"elapsed={elapsed:.2f}s")
check("result starts with the exact fallback-triggering prefix",
      result_str.startswith("SQL query failed: "), repr(result_str[:80]))
check("failure names the timeout, not some other SQL error",
      "timeout" in result_str.lower(), repr(result_str[:200]))


print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
