"""table_router.py — pick the few tables a question actually needs.

WHY THIS EXISTS
`sql_tool.execute_sql_query` sends every table's schema to the model on every
question: 12,753 input tokens at 28 tables, ~455,000 projected at 1,000
(doc-prep/CLAUDE.md, 2026-08-19; measured again in
`doc-prep/12_scale_probe.py`, 2026-08-30). That grows linearly with the
corpus and is the single thing standing between this design and the owner's
~1,000-document target. `select_tables` below is what a flagged
`execute_sql_query` calls instead of describing every table: it returns a
short, ranked list of table names, and the caller sends schema only for
those.

INPUT: table cards, not a live embedding index
`select_tables` takes `cards` — the list of dicts `doc-prep/11_table_cards.py`
builds deterministically from the manifests and CSVs already on disk
(`doc-prep/eval/table_cards.json`). Every field on a card already traces to
verified text; nothing here adds anything else. Card shape (see
`11_table_cards.py` for how each field was computed):
    table, document, row_count, columns, identifier_column,
    identifier_prefixes, one_row_is, holds, value_vocabulary, joins_to,
    caveats

WHY LEXICAL, NOT EMBEDDING, SCORING
The obvious design is "embed every card once, embed the question, take the
nearest top-k." That adds a network call and non-determinism to a function
this project needs to unit-test byte-exactly (the neighbour rule guards a
known-correct 47.39 kW answer — ls-014 — and that guard has to be provable
without a live API key). `select_tables` instead scores each card against
the question with plain token overlap over three tiers that mirror exactly
what a card carries:

  1. identifier-prefix match (weight 6) — the question names a real entity
     whose ID shape is on record, e.g. 'DB-05(B)-SP-01' starts with a
     prefix hwu_panels declared from its own data ('DB').
  2. value-vocabulary match (weight 3) — the question uses a word that is a
     literal enumerated value in some column, e.g. 'FCU' is one of
     hwu_db_circuits.load_type's <=8 distinct values.
  3. subject/column-name match (weight 1) — plain word overlap against the
     table name (de-prefixed) and its column names.

This is a deliberate, reported simplification versus the blueprint's
"embed the cards" sketch (see task-8-report.md) — swapping in a real
embedding call later means changing the scoring function's body, not this
module's contract (`select_tables(question, cards, k) -> list[str]`).

THE NEIGHBOUR RULE IS NOT OPTIONAL
Whatever scores highest, every selected table's declared `joins_to` list is
appended too. `ls-014` needs `hwu_panels` AND `hwu_smdb_feeders` in the same
schema block — a router that finds `hwu_panels` and stops there produces a
confidently wrong per-phase answer, because child boards print blank totals
in the panel schedule and the real figure lives only in the feeder schedule.
"""
import re

_STOPWORDS = {
    "the", "a", "an", "of", "is", "are", "what", "which", "how", "many",
    "for", "on", "in", "to", "and", "or", "was", "were", "does", "do",
    "give", "me", "please", "with", "at", "by", "from", "its", "this",
    "that", "there", "has", "have", "list", "show",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9()][A-Za-z0-9()\-]*")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


_ORDINAL_RE = re.compile(r"^\d+(st|nd|rd|th)?$")


def _norm_word(tok: str) -> str:
    w = tok.lower()
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]  # crude plural fold: 'FCUs' -> 'fcu', 'cameras' -> 'camera'
    return w


def _meaningful_words(tokens: list[str]) -> set[str]:
    """Shared word filter for BOTH the question and every card field, so a
    word is judged the same way on both sides of the match. Drops
    stopwords, single characters (too many coincidental collisions — 'B'
    for Block B matched almost every card's phase/status/block enum), and
    bare ordinals/numbers ('5th', '2', '01') — a floor number is not
    evidence a table is about floors; several unrelated tables in this
    corpus (CCTV floor levels, water-heater locations, panel floors) all
    print one, so on its own it is close to pure noise. The load-bearing
    'FCU' find (ls-008) survives this filter untouched; the false-positive
    'floor'/'5th' pull toward `hwu_om_cctv_camera_schedule` shrinks a lot
    (found by inspecting real scores while building this router)."""
    out = set()
    for t in tokens:
        if t.lower() in _STOPWORDS:
            continue
        w = _norm_word(t)
        if len(w) < 2 or _ORDINAL_RE.match(w):
            continue
        out.add(w)
    return out


def _question_words(question: str) -> set[str]:
    return _meaningful_words(_tokenize(question))


def _question_prefixes(question: str) -> set[str]:
    """Upper-cased lead token of every hyphenated/coded word in the
    question, e.g. 'DB-05(B)-SP-01' -> 'DB', 'CCTV-L6B-S-IDF-1' -> 'CCTV'."""
    out = set()
    for tok in _tokenize(question):
        if "-" not in tok:
            continue
        head = tok.split("-", 1)[0].strip().upper()
        if head.isalpha() and len(head) > 1:
            out.add(head)
    return out


def _card_subject_words(card: dict) -> set[str]:
    doc_prefix_tokens = {"hwu", "om"}
    name_tokens = [t for t in card["table"].split("_") if t not in doc_prefix_tokens]
    tokens = list(name_tokens)
    for col in card.get("columns", []):
        tokens += col.split("_")
    return _meaningful_words(tokens)


def _card_vocab_words(card: dict) -> set[str]:
    tokens = []
    for values in card.get("value_vocabulary", {}).values():
        for v in values:
            tokens += _tokenize(v)
    return _meaningful_words(tokens)


def _document_frequency(cards: list[dict], card_words: dict) -> dict:
    """How many cards a word appears in (subject words + vocab words
    combined) — the denominator of a simple TF-IDF-style down-weighting.

    Why this exists (found against the REAL cards, not hypothesised): a
    question about the load schedule ('FCUs on the 5th floor of Block B',
    ls-008) was outscored by CCTV's `camera_schedule` table, because that
    table's own `camera_location` enum genuinely contains the values '5TH
    FLOOR' and '4TH FLOOR' — a real, verified enum, not a false-positive
    like the one `11_table_cards.py`'s `_value_vocabulary` guard fixed. The
    words 'floor' and '5th' are simply generic enough to appear in two
    unrelated documents' real data. Down-weighting a word by how many
    tables it shows up in (so 'fcu' — one table only — outweighs 'floor' —
    several) fixes this without hand-listing generic words, the same way
    real search engines discount common terms.

    `card_words` is `select_tables`'s once-per-call {id(card): (subject,
    vocab)} map — every card's word sets are read from it rather than
    recomputed here, since every card in `cards` needs them again in
    `_score` right after this.
    """
    df = {}
    for card in cards:
        subject, vocab = card_words[id(card)]
        for w in subject | vocab:
            df[w] = df.get(w, 0) + 1
    return df


def _score(question_words: set[str], question_prefixes: set[str], card: dict,
           df: dict, card_words: dict) -> float:
    score = 0.0
    prefixes = set(card.get("identifier_prefixes", []))
    score += 6 * len(question_prefixes & prefixes)
    subject, vocab = card_words[id(card)]
    for w in question_words & vocab:
        score += 3 / df.get(w, 1)
    for w in question_words & subject:
        score += 1 / df.get(w, 1)
    return score


def select_tables(question: str, cards: list[dict], k: int = 3) -> list[str]:
    """Return up to k table names ranked by relevance to `question`, most
    relevant first, plus every declared join-neighbour of a selected table
    (neighbours are appended after the ranked top-k and are not themselves
    ranked or capped by k — the neighbour rule is a correctness guard, not
    a relevance signal, and must not be squeezed out by it).

    Deterministic: identical (question, cards, k) always returns the same
    list in the same order. Ties in score are broken by each card's
    position in `cards` (stable sort), so the result depends only on the
    inputs, never on dict/set iteration order.
    """
    if not cards:
        return []

    qwords = _question_words(question)
    qprefixes = _question_prefixes(question)
    # Each card's subject/vocab word sets are pure functions of the card,
    # but both _document_frequency and _score need them for every card —
    # computed once per card here (keyed by identity, scoped to this call
    # only) rather than twice, which matters at the corpus sizes this router
    # exists for (doc-prep/12_scale_probe.py projects 1,000 tables).
    card_words = {id(card): (_card_subject_words(card), _card_vocab_words(card))
                  for card in cards}
    df = _document_frequency(cards, card_words)

    scored = [
        (_score(qwords, qprefixes, card, df, card_words), i, card["table"])
        for i, card in enumerate(cards)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))

    by_name = {c["table"]: c for c in cards}
    top = [name for score, _, name in scored[:k] if score > 0] or \
          [name for _, _, name in scored[:k]]

    result = list(top)
    for name in top:
        for nb in by_name.get(name, {}).get("joins_to", []):
            if nb not in result and nb in by_name:
                result.append(nb)
    return result
