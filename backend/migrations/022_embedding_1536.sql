-- Migration 022: widen embeddings 768 -> 1536 dimensions.
--
-- WHY: ingestion.py asked Gemini for 768 dims with the comment
--   "Truncate to 768 dims (pgvector ivfflat max is 2000)".
-- Migration 021 replaced ivfflat with HNSW a month ago, so that reason expired.
-- gemini-embedding-001 natively produces 3072; we were storing a quarter of it.
--
-- WHY 1536 AND NOT 3072: pgvector's HNSW index tops out at 2000 dimensions. A
-- vector(3072) column is legal but CANNOT be HNSW-indexed, so every search would
-- fall back to a full sequential scan of every chunk — exactly the failure mode
-- migration 021 existed to fix. 1536 is a standard Matryoshka truncation point,
-- doubles the resolution, and keeps the index.
--
-- SCOPE: the dimension is baked into the column AND into both RPC signatures, so
-- all three change together. A parameter type change cannot be done with
-- CREATE OR REPLACE (it would create an overload), hence explicit DROPs.
--
-- The function BODIES below are copied verbatim from migration 020 — only
-- vector(768) becomes vector(1536). No logic is altered.
--
-- DATA IMPACT: dropping the embedding column discards all stored embeddings.
-- `content` and `tsv` are untouched, so keyword search keeps working throughout;
-- vector search returns nothing until the re-embed script has run. Run
-- scripts/reembed_all.py immediately after this migration.

-- ── 1. drop the two RPCs that are typed to vector(768) ──
DROP FUNCTION IF EXISTS match_document_chunks_hybrid(vector(768), TEXT, UUID, INT, JSONB, INT, TEXT, TEXT);
DROP FUNCTION IF EXISTS match_document_chunks_with_filters(vector(768), UUID, INT, JSONB, TEXT, TEXT);

-- ── 2. swap the column (768 cannot be cast to 1536) ──
DROP INDEX IF EXISTS document_chunks_embedding_hnsw_idx;
ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding;
ALTER TABLE document_chunks ADD COLUMN embedding vector(1536);

CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw_idx
  ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- ── 3. recreate match_document_chunks_with_filters at 1536 ──
CREATE OR REPLACE FUNCTION match_document_chunks_with_filters(
  query_embedding   vector(1536),
  match_user_id     UUID,
  match_count       INT     DEFAULT 5,
  metadata_filter   JSONB   DEFAULT NULL,
  match_folder_path TEXT    DEFAULT NULL,
  match_scope       TEXT    DEFAULT NULL
)
RETURNS TABLE (id UUID, document_id UUID, content TEXT, similarity FLOAT)
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  RETURN QUERY
  SELECT dc.id, dc.document_id, dc.content,
         1 - (dc.embedding <=> query_embedding) AS similarity
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

-- ── 4. recreate match_document_chunks_hybrid at 1536 ──
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
RETURNS TABLE (id UUID, document_id UUID, content TEXT, rrf_score FLOAT)
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  RETURN QUERY
  WITH vector_results AS (
    SELECT dc.id, dc.document_id, dc.content,
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
      (COALESCE(1.0 / (rrf_k + v.vector_rank), 0)
       + COALESCE(1.0 / (rrf_k + k.keyword_rank), 0))::FLOAT AS rrf_score
    FROM vector_results v
    FULL OUTER JOIN keyword_results k ON v.id = k.id
  )
  SELECT combined.id, combined.document_id, combined.content, combined.rrf_score
  FROM combined
  ORDER BY combined.rrf_score DESC
  LIMIT match_count;
END;
$$;

GRANT EXECUTE ON FUNCTION match_document_chunks_hybrid(vector(1536), TEXT, UUID, INT, JSONB, INT, TEXT, TEXT) TO authenticated;
