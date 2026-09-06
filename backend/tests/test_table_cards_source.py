"""test_table_cards_source.py — the router's cards come from the DATABASE, not the repo.

WHY THIS EXISTS, in the order the problems were found (2026-09-06):

  1. `backend/app/data/table_cards.json` had NEVER been pushed to the remote. A deploy
     from git starts with no cards at all and silently degrades to the full unrouted
     schema — so the router only ever worked on the one machine that generated the file.
  2. The committed copy had drifted FOUR tables behind the live corpus (the 167-row CCTV
     camera register and three specification tables). Nothing noticed.
  3. This repository is PUBLIC, and the cards carry sample values from a named client
     building: CCTV level codes, IDF rooms, electrical room locations.

Moving them into Supabase fixes all three. What must NOT break in the process is the
existing safety net: `sql_tool` is written so that ANY failure to obtain cards degrades
to the documented full-schema behaviour rather than raising, because a broken cards
source must never take the SQL tool down. Half of the tests below exist to hold that
line, and one of them is the whole reason this file is worth having: **the fallback must
survive the database being unreachable**, which is exactly when it is needed most.

Run:
    venv/Scripts/python -X utf8 tests/test_table_cards_source.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.sql_tool as sql_tool

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond:
        FAILS.append(name)


CARD_DB = {"table": "hwu_panels", "document": "hwu-load-schedule", "row_count": 110,
           "columns": ["panel", "tcl_kw"], "one_row_is": "one distribution board",
           "holds": "110 | from the database"}
CARD_FILE = {"table": "hwu_from_file", "document": "fallback", "row_count": 1,
             "columns": ["a"], "one_row_is": "one row", "holds": "1 | from the file"}

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


class FakeSupabase:
    """Minimal stand-in for the client's `.table(...).select(...).eq(...).execute()` chain.

    `rows_by_user` maps a user_id to the rows the database would return. `raises` makes
    every call blow up, which is how the "database unreachable" case is exercised.
    """

    def __init__(self, rows_by_user=None, raises=False):
        self.rows_by_user = rows_by_user or {}
        self.raises = raises
        self.queried_user = None
        self.calls = 0

    def table(self, name):
        assert name == "table_cards", f"unexpected table {name!r}"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        assert col == "user_id", f"cards must be scoped by user_id, got {col!r}"
        self.queried_user = val
        return self

    def execute(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("connection refused")
        return type("R", (), {"data": self.rows_by_user.get(self.queried_user, [])})()


def load(user_id, sb, path=None):
    """Call the loader under test with the cache cleared, so each case is independent."""
    sql_tool._reset_table_cards_cache()
    if path is not None:
        sql_tool._TABLE_CARDS_PATH = Path(path)
    return sql_tool._load_table_cards(user_id, sb)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cards_"))
    good_file = tmp / "cards.json"
    good_file.write_text(json.dumps([CARD_FILE]), encoding="utf-8")
    missing_file = tmp / "does_not_exist.json"
    corrupt_file = tmp / "corrupt.json"
    corrupt_file.write_text("{not json", encoding="utf-8")

    original_path = sql_tool._TABLE_CARDS_PATH
    try:
        print("1. The database is the source of truth")
        sb = FakeSupabase({USER_A: [{"table_name": "hwu_panels", "card": CARD_DB}]})
        cards = load(USER_A, sb, good_file)
        check("cards are read from the database", [c["table"] for c in cards] == ["hwu_panels"],
              f"got {[c.get('table') for c in cards]}")
        check("...and the local file is NOT used when the database has cards",
              all(c["table"] != "hwu_from_file" for c in cards))
        check("the query is scoped to the asking user", sb.queried_user == USER_A,
              f"queried {sb.queried_user}")

        print("\n2. Cards are per user — one tenant must never see another's")
        sb = FakeSupabase({
            USER_A: [{"table_name": "hwu_panels", "card": CARD_DB}],
            USER_B: [{"table_name": "other_corp", "card": dict(CARD_DB, table="other_corp")}],
        })
        a = load(USER_A, sb, good_file)
        b = load(USER_B, sb, good_file)
        check("user A sees only their own card", [c["table"] for c in a] == ["hwu_panels"])
        check("user B sees only their own card", [c["table"] for c in b] == ["other_corp"])

        print("\n3. The safety net still holds — a broken source degrades, never raises")
        sb = FakeSupabase({}, raises=True)
        cards = load(USER_A, sb, good_file)
        check("database unreachable -> falls back to the local file",
              [c["table"] for c in cards] == ["hwu_from_file"], f"got {cards}")

        sb = FakeSupabase({USER_A: []})
        cards = load(USER_A, sb, good_file)
        check("database returns no rows -> falls back to the local file",
              [c["table"] for c in cards] == ["hwu_from_file"], f"got {cards}")

        sb = FakeSupabase({}, raises=True)
        cards = load(USER_A, sb, missing_file)
        check("database unreachable AND no file -> empty list, no exception",
              cards == [], f"got {cards}")

        sb = FakeSupabase({}, raises=True)
        cards = load(USER_A, sb, corrupt_file)
        check("database unreachable AND corrupt file -> empty list, no exception",
              cards == [], f"got {cards}")

        sb = FakeSupabase({USER_A: [{"table_name": "x", "card": "not-a-dict"}]})
        cards = load(USER_A, sb, missing_file)
        check("a malformed card row does not raise", isinstance(cards, list), f"got {cards!r}")

        print("\n4. No client, no crash — the loader is usable without a database handle")
        cards = load(USER_A, None, good_file)
        check("client is None -> falls back to the local file",
              [c["table"] for c in cards] == ["hwu_from_file"], f"got {cards}")

        print("\n5. The database is not re-queried on every question")
        sb = FakeSupabase({USER_A: [{"table_name": "hwu_panels", "card": CARD_DB}]})
        sql_tool._reset_table_cards_cache()
        sql_tool._TABLE_CARDS_PATH = good_file
        sql_tool._load_table_cards(USER_A, sb)
        sql_tool._load_table_cards(USER_A, sb)
        sql_tool._load_table_cards(USER_A, sb)
        check("three loads, one database round trip", sb.calls == 1, f"made {sb.calls} calls")
    finally:
        sql_tool._TABLE_CARDS_PATH = original_path
        sql_tool._reset_table_cards_cache()

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
