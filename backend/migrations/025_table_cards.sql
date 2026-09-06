-- 025_table_cards.sql — move the router's table cards out of the code and into the database.
--
-- WHY
-- The cards are a per-corpus summary of every structured table a user owns: what each
-- table holds, its columns, and a sample of its values, so the router can pick the two or
-- three tables a question needs instead of sending the AI the schema of all of them
-- (~392,000 tokens at 1,000 tables, ~7,400 with the router).
--
-- Until now they lived in `backend/app/data/table_cards.json`, a file inside the
-- application source. Three problems with that, all of them real and all observed:
--
--   1. The file has NEVER been pushed. A deploy from git therefore starts with no cards
--      and silently falls back to the full unrouted schema — the router simply does not
--      work anywhere except the machine that generated the file.
--   2. It drifts. On 2026-09-06 the committed copy was four tables behind the live
--      corpus, missing the 167-row CCTV camera register and all three new specification
--      tables. Nothing detected this; the router was just blind to them.
--   3. This repository is PUBLIC, and the cards carry sample values from a named client
--      building — CCTV level codes, IDF room references, electrical room locations. That
--      belongs in the customer's own database, not in source control.
--
-- Storing them here fixes all three at once: private, updated the moment doc-prep
-- uploads, and one source of truth.
--
-- SHAPE
-- One row per (user_id, table_name). Cards are per-user because the thing they describe
-- is per-user: `execute_sql_query` already scopes `structured_data` by user_id, and a
-- router that could see another tenant's tables would be a data-leak surface, not a
-- feature.

CREATE TABLE IF NOT EXISTS table_cards (
    user_id     uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    table_name  text        NOT NULL,
    card        jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, table_name)
);

-- The only read the application makes is "every card for this user", so that is the
-- index. The primary key already covers it, but naming it makes the access pattern
-- explicit to anyone reading the schema later.
CREATE INDEX IF NOT EXISTS table_cards_user_idx ON table_cards (user_id);

ALTER TABLE table_cards ENABLE ROW LEVEL SECURITY;

-- Same posture as the rest of the corpus tables: a user sees only their own rows. The
-- service role bypasses RLS, which is what doc-prep's uploader uses to write them.
DROP POLICY IF EXISTS table_cards_owner_select ON table_cards;
CREATE POLICY table_cards_owner_select ON table_cards
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS table_cards_owner_write ON table_cards;
CREATE POLICY table_cards_owner_write ON table_cards
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
