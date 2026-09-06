"""
Assemble -> Verify prototype for the load-schedule board hierarchy.

READ ONLY. Writes nothing to Supabase. Prints a report and exits.

The problem it solves: `panels.fed_from` is copied verbatim from each board's own
page, so it names whatever that page printed ('DEWA', 'SMDB-BC (New)'). Many of those
strings match no board, which makes the topmost-rows total triple-count the campus.

Three stages:

  A  GATHER    every board name, every parent reference, every feeder-schedule pair
  B  ASSEMBLE  propose the real parent for each reference that is not an exact match
                 B1 deterministic - the feeder schedules already say who feeds whom
                 B2 LLM           - only for what B1 could not resolve
  C  VERIFY    deterministic tests. A proposal is auto-applicable only if the proposed
               parent's OWN page lists these boards. Name similarity alone is never
               enough - it silently mismatched 'ESMDB-C-G1 NEW' onto 'ESMDB-C-G1'
               when page 39 proves the real parent is 'ESMDB-C-G1-1'.

Usage:  cd backend && venv/Scripts/python scripts/assemble_verify.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from pydantic import BaseModel, Field
from supabase import create_client

from app.services.settings import get_llm_api_key, get_llm_model

CAMPUS_ROOT = "MDB-C"   # the plot-level board; its printed total is the ground truth


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def norm(name: str) -> str:
    """Normalise FOR COMPARISON ONLY - never for storage.

    '(New)' / '(NEW)' / 'NEW' are status labels meaning newly installed, not part of
    a board's identity. Storage always keeps the printed name.
    """
    s = str(name or "").upper()
    s = re.sub(r"\(?\s*NEW\s*\)?", "", s)
    return re.sub(r"[^A-Z0-9]", "", s)


# --------------------------------------------------------------------------
# A. GATHER
# --------------------------------------------------------------------------

def gather(sb):
    rows = sb.table("structured_data").select("table_name, rows").execute().data
    T = {t["table_name"]: t["rows"] for t in rows}
    panels, feeders = T["panels"], T["smdb_feeders"]

    panel_names = [str(r.get("panel", "")).strip() for r in panels if r.get("panel")]
    exact = set(panel_names)
    by_norm = {}
    for n in panel_names:
        by_norm.setdefault(norm(n), []).append(n)

    # "who claims to feed whom" - straight off the feeder-schedule pages
    feeds, parent_children, feeder_rows = {}, {}, {}
    for r in feeders:
        p, c = str(r.get("smdb", "")).strip(), str(r.get("feeder", "")).strip()
        if not p or not c or c.upper() in ("SPACE", "SPARE") or "FUTURE" in c.upper():
            continue
        feeds.setdefault(norm(c), []).append((p, num(r.get("tcl_kw")), r.get("source_page")))
        parent_children.setdefault(norm(p), []).append(c)
        feeder_rows.setdefault(norm(c), r)

    # boards that appear ONLY in the feeder schedules - real boards, missing a panels row
    feeder_only = {}
    for nc, r in feeder_rows.items():
        if nc not in by_norm:
            feeder_only[nc] = str(r.get("feeder", "")).strip()

    # every fed_from that is not an EXACT match to a panel name.
    # Deliberately not normalised here: a near-match is a proposal to be verified,
    # not a fact.
    unresolved = {}
    for r in panels:
        ff = str(r.get("fed_from", "")).strip()
        if ff and ff not in exact:
            unresolved.setdefault(ff, []).append(str(r.get("panel", "")).strip())

    return panels, panel_names, by_norm, feeds, parent_children, feeder_only, feeder_rows, unresolved


# --------------------------------------------------------------------------
# B1. ASSEMBLE - deterministic
# --------------------------------------------------------------------------

def assemble_deterministic(written, children, by_norm, feeds, feeder_only):
    """Feeder-schedule evidence FIRST, name similarity only as a fallback."""
    # 1. who do the feeder schedules say feeds these boards?
    claimed = {}
    for ch in children:
        for parent, _t, _p in feeds.get(norm(ch), []):
            printed, cnt = claimed.get(norm(parent), (parent, 0))
            claimed[norm(parent)] = (printed, cnt + 1)
    if len(claimed) == 1:
        nkey, (printed, cnt) = next(iter(claimed.items()))
        real = by_norm.get(nkey, [feeder_only.get(nkey, printed)])[0]
        missing = nkey not in by_norm
        return real, f"{cnt}/{len(children)} of its boards are listed under {printed!r} in the feeder schedules", \
               "high", "feeder-lookup", missing

    # 2. fallback: the written name normalises onto a board that exists
    hit = by_norm.get(norm(written))
    if hit:
        return hit[0], "name matches an existing board once '(NEW)' is ignored", \
               "medium", "name-similarity", False

    return None, "", "", "", False


# --------------------------------------------------------------------------
# B2. ASSEMBLE - LLM, only for the leftovers
# --------------------------------------------------------------------------

class Proposal(BaseModel):
    written_name: str = Field(description="the fed_from string exactly as given")
    resolution: str = Field(description="MATCH if it names a board in the candidate list, ROOT if it is an external utility supply such as a DEWA transformer, UNKNOWN if the board is not in this document at all")
    proposed_parent: str = Field(description="the board name copied exactly from the candidate list, or empty string for ROOT/UNKNOWN")
    reason: str = Field(description="one sentence citing the evidence used")
    confidence: str = Field(description="high, medium or low")


class ProposalSet(BaseModel):
    proposals: list[Proposal]


def assemble_llm(leftovers, panel_names, feeder_only, feeds):
    if not leftovers:
        return []
    from google import genai
    from google.genai import types

    lines = []
    for written, children in leftovers.items():
        ev = [f"      {ch}  --  feeder schedules list it under: "
              f"{[p for p, _, _ in feeds.get(norm(ch), [])] or 'nobody'}" for ch in children]
        lines.append(f"  {written!r} is used as the parent of {len(children)} boards:\n" + "\n".join(ev))

    prompt = f"""You are reconciling board names in an electrical load schedule.

Each board's page prints a "FED FROM:" value copied verbatim. Some of those strings
match no board. For each one, decide what it really refers to.

BOARDS WITH THEIR OWN SCHEDULE PAGE ({len(panel_names)}):
{json.dumps(sorted(panel_names))}

BOARDS THAT APPEAR ONLY IN A FEEDER SCHEDULE - real boards, no page of their own ({len(feeder_only)}):
{json.dumps(sorted(feeder_only.values()))}

UNRESOLVED PARENT REFERENCES AND THEIR EVIDENCE:
{chr(10).join(lines)}

Rules:
- Both lists above are valid candidates. Copy the name EXACTLY as written there.
- ROOT = an external utility supply (a DEWA transformer or LV board), not a board here.
- UNKNOWN = the board is not in this document at all. Prefer UNKNOWN over guessing a
  similar-looking name.
- '(New)', '(NEW)', 'NEW' are status labels meaning newly installed - not part of a
  board's identity.
- Evidence from the feeder schedules ("listed under") beats name similarity every time.
- Use confidence 'low' whenever the only evidence is that two names look alike."""

    client = genai.Client(api_key=get_llm_api_key(),
                          http_options=types.HttpOptions(timeout=180_000))
    resp = client.models.generate_content(
        model=get_llm_model(), contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0, max_output_tokens=8192,
            response_mime_type="application/json", response_schema=ProposalSet),
    )
    return ProposalSet.model_validate_json(resp.text).proposals


# --------------------------------------------------------------------------
# C. VERIFY
# --------------------------------------------------------------------------

def verify(children, parent, by_norm, feeds, parent_children, feeder_only,
           feeder_rows, panels):
    c = {}
    np_ = norm(parent or "")
    c["resolves"] = bool(parent) and (np_ in by_norm or np_ in feeder_only)

    listed = {norm(x) for x in parent_children.get(np_, [])}
    matched = [ch for ch in children if norm(ch) in listed]
    if listed:
        # the parent has a schedule page - it must name these boards
        c["two_way"] = bool(matched)
        c["detail"] = f"{len(matched)}/{len(children)} listed on the parent's own page"
    else:
        # the parent has NO page of its own (it exists only as a row in someone
        # else's feeder schedule), so there is no list to check against. Absence of
        # evidence is not evidence against - fall back to a capacity check: the
        # parent's rating must be able to cover the boards claiming to hang off it.
        prow = feeder_rows.get(np_)
        cap = num(prow.get("tcl_kw")) if prow else None
        tcl = {norm(str(r.get("panel", "")).strip()): num(r.get("tcl_kw")) for r in panels}
        load = sum(tcl.get(norm(ch)) or 0.0 for ch in children)
        c["two_way"] = cap is not None and load <= cap + 0.01
        c["detail"] = (f"parent has no page; capacity {cap} kW vs children {round(load, 3)} kW"
                       if cap is not None else "parent has no page and no rating")

    chain, cur, cyclic = set(), np_, False
    kids = {norm(ch) for ch in children}
    for _ in range(20):
        if cur in kids:
            cyclic = True
            break
        if not cur or cur in chain:
            break
        chain.add(cur)
        up = feeds.get(cur, [])
        cur = norm(up[0][0]) if up else ""
    c["no_cycle"] = not cyclic
    return c


def rollup(panels, links, virtual):
    """Topmost-row total after applying `links` and pretending `virtual` boards exist."""
    known = {norm(str(r.get("panel", "")).strip()) for r in panels if r.get("panel")}
    known |= set(virtual)
    total, roots = 0.0, []
    for r in panels:
        ff = str(r.get("fed_from", "")).strip()
        if norm(links.get(ff, ff)) in known:
            continue
        total += num(r.get("tcl_kw")) or 0.0
        roots.append((str(r.get("panel")), num(r.get("tcl_kw")) or 0.0))
    return round(total, 3), roots


# --------------------------------------------------------------------------

def main():
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    (panels, panel_names, by_norm, feeds, parent_children,
     feeder_only, feeder_rows, unresolved) = gather(sb)

    print("=" * 94)
    print("STAGE A  GATHER")
    print("=" * 94)
    print(f"  boards with their own page        : {len(panel_names)}")
    print(f"  boards only in a feeder schedule  : {len(feeder_only)}   <- real boards missing a row")
    print(f"  feeder-schedule parent->child rows: {sum(len(v) for v in parent_children.values())}")
    print(f"  parent references needing a decision: {len(unresolved)} "
          f"(used by {sum(len(v) for v in unresolved.values())} boards)")
    before_total, before_roots = rollup(panels, {}, set())
    campus = next((num(r.get("tcl_kw")) for r in panels
                   if str(r.get("panel", "")).strip() == CAMPUS_ROOT), None)
    print(f"  topmost boards right now          : {len(before_roots)}  summing to {before_total} kW")
    print(f"  {CAMPUS_ROOT}'s own printed total          : {campus} kW   <-- the correct answer")

    print("\n" + "=" * 94)
    print("STAGE B  ASSEMBLE")
    print("=" * 94)
    results, leftovers = {}, {}
    for written, children in sorted(unresolved.items()):
        parent, reason, conf, src, missing = assemble_deterministic(
            written, children, by_norm, feeds, feeder_only)
        if parent:
            results[written] = dict(parent=parent, reason=reason, confidence=conf,
                                    source=src, children=children, backfill=missing)
        else:
            leftovers[written] = children
    print(f"  B1 deterministic : resolved {len(results)} of {len(unresolved)}")
    for w, r in results.items():
        flag = "  [needs backfill]" if r["backfill"] else ""
        print(f"      {w!r:26s} -> {r['parent']:22s} [{r['source']}] {r['reason']}{flag}")

    print(f"\n  B2 LLM : {len(leftovers)} left")
    for p in assemble_llm(leftovers, panel_names, feeder_only, feeds):
        tgt = p.proposed_parent if p.resolution == "MATCH" else ""
        results[p.written_name] = dict(
            parent=tgt, reason=p.reason, confidence=p.confidence,
            source=f"llm/{p.resolution}", children=leftovers.get(p.written_name, []),
            resolution=p.resolution, backfill=bool(tgt) and norm(tgt) not in by_norm)
        print(f"      {p.written_name!r:26s} -> {(tgt or '<' + p.resolution + '>'):22s} "
              f"[{p.confidence}] {p.reason}")

    print("\n" + "=" * 94)
    print("STAGE C  VERIFY")
    print("=" * 94)
    auto, backfills, queue = {}, [], []
    for written, r in sorted(results.items()):
        if r.get("resolution") == "ROOT":
            auto[written] = ""
            print(f"  PASS   {written!r:26s} ROOT - external utility supply")
            continue
        if not r["parent"]:
            queue.append((written, r, None))
            print(f"  QUEUE  {written!r:26s} {r['reason']}")
            continue
        c = verify(r["children"], r["parent"], by_norm, feeds, parent_children,
                   feeder_only, feeder_rows, panels)
        ok = c["resolves"] and c["two_way"] and c["no_cycle"] and r["confidence"] == "high"
        print(f"  {'PASS  ' if ok else 'QUEUE '} {written!r:26s} -> {r['parent']:22s} "
              f"resolves={c['resolves']} two_way={c['two_way']} ({c['detail']}) "
              f"cycle_free={c['no_cycle']} conf={r['confidence']}")
        if ok:
            auto[written] = r["parent"]
            if r["backfill"]:
                backfills.append(r["parent"])
        else:
            queue.append((written, r, c))

    virtual = {norm(b) for b in backfills}
    after_total, after_roots = rollup(panels, auto, virtual)

    print("\n" + "=" * 94)
    print("RESULT")
    print("=" * 94)
    print(f"  auto-applicable links : {len(auto)}")
    print(f"  boards to backfill    : {len(set(backfills))}  {sorted(set(backfills))}")
    print(f"  human queue           : {len(queue)}")
    print()
    print(f"  topmost boards   {len(before_roots):>3}  ->  {len(after_roots)}")
    print(f"  campus total     {before_total} kW  ->  {after_total} kW")
    gap = round(after_total - (campus or 0), 3)
    print(f"  target ({CAMPUS_ROOT})     {campus} kW   "
          f"{'*** MATCH ***' if abs(gap) < 0.01 else f'still off by {gap}'}")
    if after_roots:
        print("\n  remaining topmost boards:")
        for n, v in after_roots:
            print(f"      {n:24s} {v}")
    if queue:
        # Cluster near-identical unresolved names so the human answers ONE question
        # about a board, not one per spelling.
        from difflib import SequenceMatcher
        groups = []
        for item in queue:
            for g in groups:
                if SequenceMatcher(None, norm(item[0]), norm(g[0][0])).ratio() >= 0.8:
                    g.append(item)
                    break
            else:
                groups.append([item])
        print(f"\n  HUMAN QUEUE - {len(groups)} question(s) from {len(queue)} unresolved names:")
        for i, g in enumerate(groups, 1):
            spellings = [w for w, _, _ in g]
            kids = sorted({ch for _, r, _ in g for ch in r["children"]})
            print(f"\n    Q{i}. What is {spellings[0]!r}?")
            if len(spellings) > 1:
                print(f"        spelled {len(spellings)} ways: {', '.join(repr(s) for s in spellings)}")
            print(f"        {len(kids)} boards hang off it: {', '.join(kids)}")
            best = next((r["parent"] for _, r, _ in g if r["parent"]), None)
            why = next((c["detail"] for _, _, c in g if c), None)
            print(f"        best guess : {best or 'not in this document'}")
            if why:
                print(f"        blocked by : {why}")
    print("\n  Nothing was written to Supabase.")


if __name__ == "__main__":
    main()
