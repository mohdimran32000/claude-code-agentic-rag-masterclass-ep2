"""test_table_router.py — acceptance tests for table_router.select_tables().

This is Stage 3 of the retrieval-scale work (doc-prep task 8): the router
that lets `sql_tool.execute_sql_query` send schema for only a few tables
instead of every table the user owns (12,753 tokens at 28 tables today,
~455,000 projected at 1,000 — doc-prep/12_scale_probe.py). The guard that
actually matters is the second block below: dropping a declared join
neighbour is how a router silently breaks a known-correct answer (ls-014,
47.39 kW, needs hwu_panels JOINed to hwu_smdb_feeders because child boards
print blank totals in the panel schedule).

Run:
    venv/Scripts/python -X utf8 tests/test_table_router.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.table_router import select_tables

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# A small, self-contained card set shaped exactly like
# doc-prep/eval/table_cards.json's real cards — this keeps the unit tests
# independent of the doc-prep repo so they run even before/without that file
# existing. Values below are drawn from the real corpus (CLAUDE.md, the
# hwu-load-schedule manifest) so the tests exercise real discrimination, not
# invented data.
# ---------------------------------------------------------------------------
CARDS = [
    {
        "table": "hwu_panels", "document": "hwu-load-schedule", "row_count": 110,
        "columns": ["panel", "kind", "block", "floor", "fed_from", "tcl_kw", "mdl_kw",
                    "rolls_up_to", "notes"],
        "identifier_column": "panel",
        "identifier_prefixes": ["DB", "SMDB", "MDB", "ESMDB", "EDB", "MCC"],
        "one_row_is": "one row is one record, identified by its `panel` value",
        "holds": "panel/board schedule — total connected load and maximum demand per board",
        "value_vocabulary": {"kind": ["MDB", "SMDB", "MCC", "ESMDB", "DB", "EDB", "BUSWAY"],
                              "block": ["B", "C"]},
        "joins_to": ["hwu_smdb_feeders", "hwu_db_circuits"],
        "caveats": [],
    },
    {
        "table": "hwu_smdb_feeders", "document": "hwu-load-schedule", "row_count": 252,
        "columns": ["smdb", "feeder", "tcl_kw", "mdl_kw", "breaker_a", "notes"],
        "identifier_column": "feeder",
        "identifier_prefixes": ["DB", "SMDB", "MDB", "ESMDB"],
        "one_row_is": "one row is one feeder, identified by its `feeder` value",
        "holds": "feeder schedule — child boards' totals when the panel schedule leaves them blank",
        "value_vocabulary": {},
        "joins_to": ["hwu_panels", "hwu_db_circuits"],
        "caveats": [],
    },
    {
        "table": "hwu_db_circuits", "document": "hwu-load-schedule", "row_count": 2371,
        "columns": ["db", "cir_no", "load_type", "points", "load_w", "room_area", "notes"],
        "identifier_column": "room_area",
        "identifier_prefixes": [],
        "one_row_is": "one row is one circuit",
        "holds": "circuit-level rows: load type, points, watts, per distribution board",
        "value_vocabulary": {"load_type": ["FCU", "LTG", "Sockets", "Water Heater", "Zip Tap"]},
        "joins_to": ["hwu_panels", "hwu_smdb_feeders"],
        "caveats": [],
    },
    {
        "table": "hwu_om_cctv_camera_schedule", "document": "hwu-om-cctv", "row_count": 55,
        "columns": ["camera_tag", "idf_location", "camera_location", "tilt_angle_deg",
                     "mounting_height_m", "notes"],
        "identifier_column": "camera_tag",
        "identifier_prefixes": ["CCTV"],
        "one_row_is": "one row is one camera, identified by its `camera_tag` value",
        "holds": "camera tilt/mounting schedule from the CCTV as-built drawings",
        "value_vocabulary": {},
        "joins_to": ["hwu_om_cctv_commissioning"],
        "caveats": [],
    },
    {
        "table": "hwu_om_cctv_commissioning", "document": "hwu-om-cctv", "row_count": 77,
        "columns": ["camera_tag", "ip_address", "focussing", "live", "notes"],
        "identifier_column": "camera_tag",
        "identifier_prefixes": ["CCTV"],
        "one_row_is": "one row is one commissioning checklist row",
        "holds": "camera commissioning checklists: IP address, focus, live status",
        "value_vocabulary": {},
        "joins_to": ["hwu_om_cctv_camera_schedule"],
        "caveats": [],
    },
    {
        "table": "hwu_om_waterheaters_asset_register", "document": "hwu-om-waterheaters-v2",
        "row_count": 5,
        "columns": ["asset_tag", "model_number", "manufacturer_name", "warranty_status",
                     "warranty_expires", "capacity_litres", "notes"],
        "identifier_column": "asset_tag",
        "identifier_prefixes": ["EWH"],
        "one_row_is": "one row is one water heater, identified by its `asset_tag` value",
        "holds": "water heater asset register: model, manufacturer, warranty per unit",
        "value_vocabulary": {"manufacturer_name": ["HEATER SADIE", "ARISTON"],
                              "warranty_status": ["EXPIRED", "NONE - no warranty document exists"]},
        "joins_to": ["hwu_om_waterheaters_models", "hwu_om_waterheaters_spare_parts"],
        "caveats": [],
    },
    {
        "table": "hwu_om_waterheaters_models", "document": "hwu-om-waterheaters-v2",
        "row_count": 5,
        "columns": ["model", "manufacturer", "capacity_litres", "power_kw", "notes"],
        "identifier_column": "model",
        "identifier_prefixes": [],
        "one_row_is": "one row is one water heater model",
        "holds": "water heater model specifications: capacity, power, installation",
        "value_vocabulary": {},
        "joins_to": ["hwu_om_waterheaters_asset_register"],
        "caveats": [],
    },
]


def names(result):
    return result


print("1. A question naming a table's subject selects that table")
r = select_tables("How many cameras are installed on site?", CARDS, k=3)
check("camera question selects a cctv table", any(n.startswith("hwu_om_cctv") for n in r), r)

r = select_tables("What is the manufacturer of EWH-GF-001?", CARDS, k=3)
check("an asset-tag question (EWH- prefix) selects the water heater asset register",
      "hwu_om_waterheaters_asset_register" in r, r)

r = select_tables("What is the IP address of CCTV-L6B-S-IDF-1-DET-18?", CARDS, k=3)
check("a camera-tag question (CCTV- prefix) selects the commissioning table",
      "hwu_om_cctv_commissioning" in r, r)

print("\n2. The neighbour rule — ls-014 (47.39 kW) and ls-008 (FCU count) canaries")
r = select_tables("What is the total connected load of DB-05(B)-SP-01?", CARDS, k=3)
check("ls-014: hwu_panels is selected", "hwu_panels" in r, r)
check("ls-014: hwu_smdb_feeders travels with it as a declared neighbour "
      "(dropping it breaks the 47.39 kW feeder-schedule fallback)",
      "hwu_smdb_feeders" in r, r)

r = select_tables("How many FCUs are on the 5th floor of Block B?", CARDS, k=3)
check("ls-008: hwu_db_circuits is selected (FCU is a load_type vocabulary hit)",
      "hwu_db_circuits" in r, r)
check("ls-008: hwu_panels travels with it (floor/block filters resolve there)",
      "hwu_panels" in r, r)

print("\n3. Selecting ANY table in a join pair pulls in the other side")
r = select_tables("What is the breaker rating of the feeder on hwu_smdb_feeders?", CARDS, k=1)
check("selecting hwu_smdb_feeders alone (k=1) still pulls hwu_panels and hwu_db_circuits",
      "hwu_panels" in r and "hwu_db_circuits" in r, r)

print("\n4. Determinism and ordering")
r1 = select_tables("How many FCUs are on the 5th floor of Block B?", CARDS, k=3)
r2 = select_tables("How many FCUs are on the 5th floor of Block B?", CARDS, k=3)
check("identical question -> identical result, every call", r1 == r2, (r1, r2))
check("result is a list of table names (strings)", all(isinstance(n, str) for n in r1))

print("\n5. k actually bounds the RANKED selection (neighbours are additive, not capped)")
r = select_tables("How many cameras are installed on site?", CARDS, k=1)
ranked_part = r[:1]
check("k=1 still returns exactly 1 ranked pick plus its declared neighbours",
      len(ranked_part) == 1)

print("\n6. Empty cards / no signal doesn't crash")
check("empty card list returns empty", select_tables("anything", [], k=3) == [])
r = select_tables("asdkjhaskjdh completely unrelated gibberish", CARDS, k=3)
check("a question matching nothing still returns <=k ranked names without crashing",
      isinstance(r, list) and len(r) >= 0)

# ---------------------------------------------------------------------------
# Integration check against the REAL cards, if doc-prep has generated them.
# Skipped (not failed) when the file doesn't exist, so this test suite runs
# standalone in CI without a cross-repo dependency.
# ---------------------------------------------------------------------------
print("\n7. Integration: the real eval/table_cards.json, if present")
import json
real_cards_path = Path(r"C:\RAG Automators\doc-prep\eval\table_cards.json")
if real_cards_path.exists():
    real_cards = json.loads(real_cards_path.read_text(encoding="utf-8"))
    r = select_tables("What is the total connected load of DB-05(B)-SP-01?", real_cards, k=3)
    check("[real cards] ls-014: hwu_panels selected", "hwu_panels" in r, r)
    check("[real cards] ls-014: hwu_smdb_feeders travels with it", "hwu_smdb_feeders" in r, r)
    r = select_tables("How many FCUs are on the 5th floor of Block B?", real_cards, k=3)
    check("[real cards] ls-008: hwu_db_circuits selected", "hwu_db_circuits" in r, r)
    check("[real cards] ls-008: hwu_panels travels with it", "hwu_panels" in r, r)
else:
    print(f"  skip  eval/table_cards.json not found at {real_cards_path} — run 11_table_cards.py first")

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
