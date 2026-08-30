-- Migration 023: chunk identity — stamp every chunk with where it came from.
--
-- WHY: eval/results.jsonl (2026-08-29 tuning split) measured recall@48 = 100% and
-- recall@12 = 100% — the right chunk is always fetched and always survives
-- reranking — but decoy-beaten = 50%. Every retrieval failure was DECOY_ABOVE: a
-- look-alike passage from a DIFFERENT document outranking the correct one. Search
-- is not the problem; discrimination is. This migration is the schema half of the
-- fix — the app half (ingestion.py: identity_header(), title= at embed time) is
-- gated behind retrieval_flags.flag("chunk_identity") and lands in the same PR.
--
-- 🔴 READ THIS BEFORE TOUCHING THE TWO RPCs BELOW — signature-stability rule:
-- Migration 022 had to DROP both match_document_chunks_* functions before
-- recreating them, because changing an INPUT parameter's type (vector(768) ->
-- vector(1536)) with a same-named, same-arg-count CREATE OR REPLACE creates an
-- AMBIGUOUS OVERLOAD instead of replacing anything — Postgres resolves a call by
-- (name, input arg types) only, so two functions can silently coexist and every
-- call becomes "function is not unique", or worse, resolves to the wrong one.
--
-- This migration does NOT touch any input parameter — but it DOES change the
-- RETURN shape (RETURNS TABLE gains 7 columns). Postgres's rule for that case is
-- different again: CREATE OR REPLACE FUNCTION refuses to change the output-column
-- list of an existing RETURNS TABLE / OUT-parameter function outright (it raises
-- "cannot change return type of existing function", not a silent overload) UNLESS
-- the function is dropped first. Rather than depend on that being a loud error in
-- every Postgres version, we apply the SAME explicit-DROP discipline migration 022
-- used for input types, here for the return type. Get this wrong in either
-- direction — an overload for inputs, or a refused/partial replace for outputs —
-- and hybrid search silently returns nothing, which is exactly how the migration
-- 021 ivfflat bug behaved before anyone noticed.
--
-- SCOPE: only /om-waterheaters and /om-firefighting get re-ingested with the flag
-- on (per the experiment plan) — every other folder's existing rows keep NULL in
-- the 7 new columns until they are re-ingested. NULL is a valid, expected state,
-- not an error: both RPCs below select the columns as plain passthroughs and the
-- app already treats an absent identity as "omit it" (identity_header() itself
-- treats None the same way).
--
-- DATA IMPACT: none. ADD COLUMN IF NOT EXISTS only adds nullable columns; no
-- existing row is rewritten, no embedding is touched, no re-embed is required by
-- this migration alone (chunk_identity's title= change DOES require re-embedding
-- the two target folders, but that is the app-level re-ingest step, not this SQL).

-- ── 1. document_chunks: add the 7 identity columns ──
ALTER TABLE document_chunks
  ADD COLUMN IF NOT EXISTS file_name     TEXT,
  ADD COLUMN IF NOT EXISTS folder_path   TEXT,
  ADD COLUMN IF NOT EXISTS doc_title     TEXT,
  ADD COLUMN IF NOT EXISTS page_start    INT,
  ADD COLUMN IF NOT EXISTS page_end      INT,
  ADD COLUMN IF NOT EXISTS section_path  TEXT,
  ADD COLUMN IF NOT EXISTS tags          TEXT[];

CREATE INDEX IF NOT EXISTS document_chunks_tags_gin_idx
  ON document_chunks USING GIN (tags);

-- ── 2. drop the two RPCs — return-shape change, see header comment ──
DROP FUNCTION IF EXISTS match_document_chunks_hybrid(vector(1536), TEXT, UUID, INT, JSONB, INT, TEXT, TEXT);
DROP FUNCTION IF EXISTS match_document_chunks_with_filters(vector(1536), UUID, INT, JSONB, TEXT, TEXT);

-- ── 3. recreate match_document_chunks_with_filters, +7 identity columns ──
-- Same body as migration 022 (verbatim), only the RETURNS TABLE list and the
-- final SELECT grew — no WHERE/ORDER/JOIN logic changed.
CREATE FUNCTION match_document_chunks_with_filters(
  query_embedding   vector(1536),
  match_user_id     UUID,
  match_count       INT     DEFAULT 5,
  metadata_filter   JSONB   DEFAULT NULL,
  match_folder_path TEXT    DEFAULT NULL,
  match_scope       TEXT    DEFAULT NULL
)
RETURNS TABLE (
  id UUID, document_id UUID, content TEXT, similarity FLOAT,
  file_name TEXT, folder_path TEXT, doc_title TEXT,
  page_start INT, page_end INT, section_path TEXT, tags TEXT[]
)
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  RETURN QUERY
  SELECT dc.id, dc.document_id, dc.content,
         1 - (dc.embedding <=> query_embedding) AS similarity,
         dc.file_name, dc.folder_path, dc.doc_title,
         dc.page_start, dc.page_end, dc.section_path, dc.tags
  FROM document_chunks dc
  JOIN public.documents d ON dc.document_id = d.id
  WHERE dc.user_id = match_user_id
    AND dc.embedding IS NOT NULL
    AND (metadata_filter IS NULL OR d.metadata @> metadata_filter)
    AND (match_folder_path IS NULL
         OR d.folder_path = match_folder_path
         OR d.folder_path LIKE match_folder_path
            || (CASE WHEN match_folder_path = '/' THEN '%' ELSE '/%' END))
    AND (match_scope IS NULL OR d.scope = match_scope)
  ORDER BY dc.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

GRANT EXECUTE ON FUNCTION match_document_chunks_with_filters(vector(1536), UUID, INT, JSONB, TEXT, TEXT) TO authenticated;

-- ── 4. recreate match_document_chunks_hybrid, +7 identity columns ──
-- Same RRF body as migration 022 (verbatim) — the CTEs now carry the identity
-- columns through so they survive the FULL OUTER JOIN's COALESCE.
CREATE FUNCTION match_document_chunks_hybrid(
  query_embedding    vector(1536),
  query_text         TEXT,
  match_user_id      UUID,
  match_count        INT     DEFAULT 20,
  metadata_filter    JSONB   DEFAULT NULL,
  rrf_k              INT     DEFAULT 60,
  match_folder_path TEXT     DEFAULT NULL,
  match_scope       TEXT     DEFAULT NULL
)
RETURNS TABLE (
  id UUID, document_id UUID, content TEXT, rrf_score FLOAT,
  file_name TEXT, folder_path TEXT, doc_title TEXT,
  page_start INT, page_end INT, section_path TEXT, tags TEXT[]
)
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  RETURN QUERY
  WITH vector_results AS (
    SELECT dc.id, dc.document_id, dc.content,
           dc.file_name, dc.folder_path, dc.doc_title,
           dc.page_start, dc.page_end, dc.section_path, dc.tags,
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> query_embedding) AS vector_rank
    FROM document_chunks dc
    JOIN public.documents d ON dc.document_id = d.id
    WHERE dc.user_id = match_user_id
      AND dc.embedding IS NOT NULL
      AND (metadata_filter IS NULL OR d.metadata @> metadata_filter)
      AND (match_folder_path IS NULL
           OR d.folder_path = match_folder_path
           OR d.folder_path LIKE match_folder_path
              || (CASE WHEN match_folder_path = '/' THEN '%' ELSE '/%' END))
      AND (match_scope IS NULL OR d.scope = match_scope)
    ORDER BY dc.embedding <=> query_embedding
    LIMIT match_count * 2
  ),
  keyword_results AS (
    SELECT dc.id, dc.document_id, dc.content,
           dc.file_name, dc.folder_path, dc.doc_title,
           dc.page_start, dc.page_end, dc.section_path, dc.tags,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.tsv, websearch_to_tsquery('english', query_text)) DESC) AS keyword_rank
    FROM document_chunks dc
    JOIN public.documents d ON dc.document_id = d.id
    WHERE dc.user_id = match_user_id
      AND dc.tsv IS NOT NULL
      AND dc.tsv @@ websearch_to_tsquery('english', query_text)
      AND (metadata_filter IS NULL OR d.metadata @> metadata_filter)
      AND (match_folder_path IS NULL
           OR d.folder_path = match_folder_path
           OR d.folder_path LIKE match_folder_path
              || (CASE WHEN match_folder_path = '/' THEN '%' ELSE '/%' END))
      AND (match_scope IS NULL OR d.scope = match_scope)
    ORDER BY ts_rank_cd(dc.tsv, websearch_to_tsquery('english', query_text)) DESC
    LIMIT match_count * 2
  ),
  combined AS (
    SELECT
      COALESCE(v.id, k.id) AS id,
      COALESCE(v.document_id, k.document_id) AS document_id,
      COALESCE(v.content, k.content) AS content,
      COALESCE(v.file_name, k.file_name) AS file_name,
      COALESCE(v.folder_path, k.folder_path) AS folder_path,
      COALESCE(v.doc_title, k.doc_title) AS doc_title,
      COALESCE(v.page_start, k.page_start) AS page_start,
      COALESCE(v.page_end, k.page_end) AS page_end,
      COALESCE(v.section_path, k.section_path) AS section_path,
      COALESCE(v.tags, k.tags) AS tags,
      (COALESCE(1.0 / (rrf_k + v.vector_rank), 0)
       + COALESCE(1.0 / (rrf_k + k.keyword_rank), 0))::FLOAT AS rrf_score
    FROM vector_results v
    FULL OUTER JOIN keyword_results k ON v.id = k.id
  )
  SELECT combined.id, combined.document_id, combined.content, combined.rrf_score,
         combined.file_name, combined.folder_path, combined.doc_title,
         combined.page_start, combined.page_end, combined.section_path, combined.tags
  FROM combined
  ORDER BY combined.rrf_score DESC
  LIMIT match_count;
END;
$$;

GRANT EXECUTE ON FUNCTION match_document_chunks_hybrid(vector(1536), TEXT, UUID, INT, JSONB, INT, TEXT, TEXT) TO authenticated;

-- ── 5. keyword-index weighting: header must not flatten ts_rank_cd ──
-- The identity header repeats the SAME doc_title/file_name/page substring across
-- every chunk of a document. If it were indexed at the same weight as the body,
-- a query that happens to match header tokens would rank chunks by document
-- membership rather than by content — the opposite of what chunk_identity is for.
-- Postgres weights (highest to lowest) are A=1.0, B=0.4, C=0.2, D=0.1. Every
-- lexeme from a plain to_tsvector() call (no setweight()) is already implicitly
-- weight D, which is what document_chunks_tsv_trigger() has produced since
-- migration 008 — so this is not a downgrade for existing content, it is
-- upgrading the BODY to weight A and leaving a detected header at D.
--
-- Header detection is deliberately NOT a bracket-only regex: content that
-- legitimately starts with a bracketed phrase (e.g. an inline "[Image: ...]"
-- description already present in this corpus) must not be misread as an identity
-- header and demoted. The header's own "·" (U+00B7) separator is the signature we
-- require in addition to the brackets, which our own header always contains and
-- ordinary prose essentially never does. When flag OFF content has no such first
-- line, header_line is NULL and the whole chunk is indexed as body at weight A —
-- a strict superset of the old (uniform D) behaviour, so relative ranking within
-- any single query is unaffected for un-stamped chunks.
CREATE OR REPLACE FUNCTION document_chunks_tsv_trigger() RETURNS trigger AS $$
DECLARE
  first_line text := split_part(NEW.content, chr(10), 1);
  header_line text;
  body_text text;
BEGIN
  IF first_line LIKE '[%]' AND position('·' IN first_line) > 0 THEN
    header_line := first_line;
    body_text := substring(NEW.content FROM length(first_line) + 2);
  ELSE
    header_line := NULL;
    body_text := NEW.content;
  END IF;

  NEW.tsv :=
    setweight(to_tsvector('english', COALESCE(header_line, '')), 'D') ||
    setweight(to_tsvector('english', COALESCE(body_text, '')), 'A');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- No CREATE TRIGGER needed — migration 008's trg_document_chunks_tsv trigger is
-- already bound to this function by name and picks up the new body immediately.
-- No backfill UPDATE either: only rows inserted/updated after this migration
-- (i.e. the om-waterheaters/om-firefighting re-ingest) run the new logic: tsv on
-- every other row is untouched, so no other folder's keyword ranking moves.
