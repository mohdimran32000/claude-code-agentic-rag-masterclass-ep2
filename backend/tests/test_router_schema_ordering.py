"""
Guards two live defects (task-6b):

1. messages.py's router-table menu fetch used .limit(20) with no .order() --
   on a 28-table corpus, 8 tables were invisible to the router and *which*
   eight was non-deterministic. Fix: drop the cap, order by table_name.
2. sql_tool.py's schema fetch had no .order() -- the schema block's byte
   content varied between requests, defeating Gemini's implicit prompt cache.
   Fix: .order("table_name"), unconditional.

Both are exercised with a fake query-chain object that records what was
called, so the test needs no network and no real Supabase.
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond: FAILS.append(name)


# ---------------------------------------------------------------- sql_tool.py
print("sql_tool.execute_sql_query() -- deterministic schema ordering")

class _SchemaQuery:
    """Records the query chain so the test can assert what production sends."""
    def __init__(self, rows):
        self.rows = rows
        self.order_applied = None
        self.limit_applied = None
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, col, **k):
        self.order_applied = col
        return self
    def limit(self, n):
        self.limit_applied = n
        return self
    def execute(self):
        rows = sorted(self.rows, key=lambda r: r["table_name"]) if self.order_applied else self.rows
        class R: data = rows
        return R()

class _SchemaSB:
    def __init__(self, rows): self.q = _SchemaQuery(rows)
    def table(self, name): return self.q

from app.services import sql_tool as st

# Empty table list => execute_sql_query returns right after the fetch, before
# ever reaching SQL generation -- so this test makes no LLM/network call.
sb = _SchemaSB([])
result = st.execute_sql_query("any question", "user1", sb)
check("schema fetch is ORDERED for byte-stable prompt caching",
      sb.q.order_applied == "table_name", f"order={sb.q.order_applied}")
check("early-return path is unaffected (no tables -> friendly message)",
      result == "No tabular data found. Upload a CSV or XLSX file first.", result)


# ---------------------------------------------------------------- messages.py
print("messages.send_message() -- router table menu: no cap, ordered")

class _ThreadQuery:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def maybe_single(self): return self
    def execute(self):
        class R: data = {"id": "t1"}
        return R()

class _MessagesQuery:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, *a, **k): return self
    def execute(self):
        class R: data = []
        return R()

class _DocumentsQuery:
    def select(self, *a, **k): return self
    def or_(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        class R: data = []
        return R()

class _RouterQuery:
    """The one that matters: structured_data, the router's table menu."""
    def __init__(self, rows):
        self.rows = rows
        self.order_applied = None
        self.limit_applied = None
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, col, **k):
        self.order_applied = col
        return self
    def limit(self, n):
        self.limit_applied = n
        return self
    def execute(self):
        rows = sorted(self.rows, key=lambda r: r["table_name"]) if self.order_applied else self.rows
        if self.limit_applied is not None:
            rows = rows[: self.limit_applied]
        class R: data = rows
        return R()

class _FakeSupabase:
    def __init__(self, structured_rows):
        self.router_query = _RouterQuery(structured_rows)
        self._tables = {
            "threads": _ThreadQuery(),
            "messages": _MessagesQuery(),
            "documents": _DocumentsQuery(),
            "structured_data": self.router_query,
        }
    def table(self, name):
        return self._tables[name]

from app.routers import messages as msg_mod
from app.models.schemas import MessageCreate

rows_28 = [{"table_name": f"hwu_t{i:02d}", "columns": ["a"]} for i in range(28)]
fake_sb = _FakeSupabase(rows_28)
msg_mod.get_supabase_client = lambda: fake_sb

body = MessageCreate(content="how many rows in hwu_t27?")
asyncio.run(msg_mod.send_message(thread_id="t1", body=body, user_id="u1"))

check("router menu fetch is ORDERED, so any cap would be deterministic",
      fake_sb.router_query.order_applied == "table_name",
      f"order={fake_sb.router_query.order_applied}")
check("router menu fetch has NO cap -- all 28 tables reach the model",
      fake_sb.router_query.limit_applied is None,
      f"limit={fake_sb.router_query.limit_applied}")


print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
