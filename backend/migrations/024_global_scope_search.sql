-- Migration 024: make scope='global' documents actually reachable by search.
--
-- WHY: migration 012's CHECK constraint (documents_scope_user_id_consistency)
-- gives a global document user_id IS NULL by design:
--
--   CHECK ((scope = 'user'   AND user_id IS NOT NULL)
--       OR (scope = 'global' AND user_id IS NULL))
--
-- ...and document_chunks denormalizes the same NULL user_id onto every chunk of
-- that document (migration 015 §5). But both search RPCs — recreated most
-- recently by migration 023 — filter their candidate sets with:
--
--   WHERE dc.user_id = match_user_id
--
-- In SQL, NULL = <anything> is never TRUE (it's UNKNOWN, which WHERE treats as
-- reject). So a global chunk's dc.user_id (NULL) can never equal any caller's
-- match_user_id (a real UUID) — global documents are structurally excluded
-- from every search result, for every user, always. The feature was never
-- reachable; it has been dormant only because nothing has been marked global
-- yet (creating one requires public.is_admin(), migration 015).
--
-- THE FIX: admit a row when it is EITHER the caller's own chunk OR a global
-- chunk, by widening the ownership leg of the predicate:
--
--   BEFORE:  WHERE dc.user_id = match_user_id
--   AFTER:   WHERE (dc.user_id = match_user_id OR d.scope = 'global')
--
-- Applied to all three occurrences of the ownership predicate: the single
-- WHERE in match_document_chunks_with_filters, and both CTEs (vector_results,
-- keyword_results) inside match_document_chunks_hybrid.
--
-- 🔴 SECURITY WALK-THROUGH — read before touching this file again.
--
-- The service uses the Supabase SERVICE ROLE key for every RPC call
-- (backend/app/auth.py:get_supabase_client()), and the service role has
-- BYPASSRLS. That means the RLS policies in migration 015
-- (documents_select / document_chunks_select) do NOT run for these calls —
-- the WHERE clause inside these SECURITY INVOKER functions is the ONLY access
-- control standing between one user's query and another user's private rows.
-- This change has to be judged on its own, not on RLS backing it up.
--
-- Claim: OR d.scope = 'global' cannot expose a private (scope='user') document
-- belonging to a different user.
--
-- Proof, by cases, for a chunk belonging to some document D with match_user_id
-- = A (the caller) and D.user_id = B (some other user), B <> A:
--
--   Case D.scope = 'user'  (an ordinary private document):
--     By documents_scope_user_id_consistency, scope='user' FORCES
--     D.user_id IS NOT NULL — here that is B.
--     Leg 1: dc.user_id = match_user_id  ->  B = A  ->  FALSE (B <> A).
--     Leg 2: d.scope = 'global'          ->  FALSE  (it's 'user').
--     FALSE OR FALSE = FALSE  ->  row excluded.  No change from before this
--     migration: this is exactly the predicate's existing behaviour.
--
--   Case D.scope = 'global':
--     By the SAME constraint, scope='global' FORCES D.user_id IS NULL — there
--     is no "other user" here to leak from; nobody owns this row privately.
--     It is, by construction, the shared document the feature exists to serve.
--     Leg 2 alone admits it, for every caller — which is the intended,
--     documented behaviour of a global document.
--
-- The predicate reads d.scope (the JOINed `documents` row, where the CHECK
-- constraint lives and is enforced), never dc.scope (document_chunks' own
-- denormalized copy) — so the safety argument rests on the one column the
-- database itself guarantees is coupled to user_id, not on a copy that could
-- in principle drift from it.
--
-- What is deliberately UNCHANGED and still gates the other direction: only an
-- admin can ever create or flip a document to scope='global' in the first
-- place (documents_insert_global / documents_update_global RLS policies,
-- migration 015, both require public.is_admin() and enforce user_id IS NULL);
-- and forbid_scope_mutation() blocks changing scope on an existing row at all.
-- Those writes still go through the authenticated-role path with RLS active,
-- so this migration does not need to (and does not) touch them.
--
-- match_scope keeps working unchanged: it is a separate, later condition
-- (AND (match_scope IS NULL OR d.scope = match_scope)) that narrows the result
-- AFTER the ownership predicate has already decided visibility — a caller who
-- explicitly asks for match_scope='user' still only gets their own rows, and
-- a caller who asks for match_scope='global' still only gets shared rows.
--
-- SIGNATURE-STABILITY: this migration changes ONLY the function bodies (the
-- WHERE-clause text inside each CTE). Every input parameter (name, type,
-- order, default) and the RETURNS TABLE column list are byte-identical to
-- migration 023. Per the signature-stability rule established in 023's own
-- header comment, a body-only change is exactly the case where
-- CREATE OR REPLACE FUNCTION is safe and correct — Postgres only refuses (or
-- silently overloads) REPLACE when input types or the output column list
-- change, neither of which happens here. DROP FUNCTION is therefore NOT used,
-- and existing GRANTs are not dropped by this migration — CREATE OR REPLACE
-- preserves them. The GRANT EXECUTE statements below are reissued anyway,
-- defensively, so this file is self-sufficient and matches the visible
-- pattern in 022/023 rather than relying on that preservation being obvious
-- to the next reader.
--
-- DATA IMPACT: none. No column, index, row, or embedding changes. Purely a
-- widening of a search predicate.
--
-- TRANSACTION WRAPPER: wrapped in BEGIN/COMMIT for the same reason as 023 —
-- Postgres DDL is transactional, and if anything failed between replacing the
-- first function and the second, an unwrapped run could leave the live system
-- with one function on the old (broken) predicate and one on the new one, or
-- (with a DROP-based migration) no function at all. CREATE OR REPLACE cannot
-- leave "no function", but the wrapper costs nothing and keeps this file
-- consistent with 023's discipline.
--
-- 023 is already applied to the live database; this migration layers on top
-- of it and does not restate or undo anything in 023.

BEGIN;

-- ── 1. match_document_chunks_with_filters: widen the ownership leg ──
-- Body otherwise byte-identical to migration 023.
CREATE OR REPLACE FUNCTION match_document_chunks_with_filters(
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
  WHERE (dc.user_id = match_user_id OR d.scope = 'global')
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

-- ── 2. match_document_chunks_hybrid: widen the ownership leg in BOTH legs ──
-- Body otherwise byte-identical to migration 023 — vector_results and
-- keyword_results each get the same widened predicate as above.
CREATE OR REPLACE FUNCTION match_document_chunks_hybrid(
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
    WHERE (dc.user_id = match_user_id OR d.scope = 'global')
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
    WHERE (dc.user_id = match_user_id OR d.scope = 'global')
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

COMMIT;
