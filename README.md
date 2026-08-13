# Talk to the Building

An agentic RAG assistant for building operations. Upload O&M manuals, electrical load schedules, maintenance contracts, and drawings — then ask questions in plain English and get grounded, cited answers.

> *"What's the total connected load for Block B?"* → routed to text-to-SQL over the ingested load schedules → `1,234.30 kW` with a per-panel breakdown.

Built entirely by collaborating with Claude Code — no hand-written code. Started from [The AI Automators' Agentic RAG Masterclass](https://www.youtube.com/watch?v=xgPWCuqLoek) and extended well beyond the course.

## What It Does

**Chat**
- Threaded conversations with SSE streaming, stop button, and markdown rendering
- Tool-activity display — see which tool the agent picked and why
- Collapsible sub-agent reasoning sections
- Dark / light / system theme

**Documents**
- Drag-and-drop upload with live processing status (polling)
- File explorer with folders, breadcrumbs, and context menus
- Two scopes: personal documents and shared (global) documents, enforced by Row-Level Security
- Metadata badges (document type, topic, keywords, entities…) extracted by the LLM at ingest

**Ingestion pipeline**
- [Docling](https://github.com/docling-project/docling) parsing — layout-aware, OCR, 30+ formats (PDF, DOCX, PPTX, XLSX, images…)
- Record Manager deduplication: identical file → skip, changed file → re-ingest, new file → create
- CSV/XLSX rows also land in a `structured_data` table for SQL querying (smart header detection, typed columns)
- Chunking → `gemini-embedding-001` embeddings (768 dims) → pgvector

**Retrieval**
- Hybrid search: vector (pgvector) + keyword (tsvector) fused with Reciprocal Rank Fusion
- Optional reranking: Gemini (LLM-as-judge) or Cohere Rerank API
- Metadata filtering — manual via UI filter bar, or agentic (the LLM picks filters itself)

**Agentic tools** — the LLM decides per message which tool (if any) to call:

| Tool | What it does |
|---|---|
| `search_documents` | Hybrid semantic + keyword search over document chunks |
| `query_structured_data` | Text-to-SQL over ingested spreadsheets (DuckDB in-memory) |
| `web_search` | Tavily web search fallback |
| `analyze_document` | Sub-agent reads an entire document in isolated context |
| Explorer sub-agent | 5 file-system-style tools: `tree`, `list_files`, `glob`, `grep`, `read_document` |

**Admin settings UI**
- Model picker (fetched live from the Gemini API), LLM + LangSmith config
- Toggles for hybrid search, reranking (+ provider), text-to-SQL, web search
- Editable metadata extraction schema

## Architecture

```
User message
     │
     ▼
Gemini call #1 (non-streaming, tools enabled) — picks a tool, or answers directly
     │
     ├── search_documents ──► embed query → hybrid RPC (vector + keyword + RRF) → rerank
     ├── query_structured_data ──► generate SQL → DuckDB over structured_data → result table
     ├── web_search ──► Tavily API → formatted results with sources
     └── analyze_document ──► sub-agent Gemini call with full document context
     │
     ▼
Tool result injected into system prompt
     │
     ▼
Gemini call #2 (streaming, no tools) ──► answer streamed to the browser via SSE
```

Design principles (see [CLAUDE.md](./CLAUDE.md)):
- **No LangChain / LangGraph** — raw SDK calls, hand-rolled orchestration in plain Python
- **Stateless completions** — chat history stored in Postgres and sent explicitly
- **RLS everywhere** — users only ever see their own data (plus shared global docs)

## Tech Stack

| Layer | Tech |
|---|---|
| Frontend | React, TypeScript, Vite, Tailwind v4, shadcn/ui |
| Backend | Python, FastAPI, SSE (sse-starlette) |
| Database | Supabase — Postgres, pgvector, Auth, Storage, RLS |
| LLM | Google Gemini via native `google-genai` SDK (model configurable in admin settings) |
| Doc parsing | Docling (layout analysis + OCR, GPU-accelerated) |
| Text-to-SQL | DuckDB (in-memory) |
| Web search | Tavily |
| Reranking | Gemini or Cohere (optional) |
| Observability | LangSmith |

## Testing & Evals

| Suite | Size | Command |
|---|---|---|
| Backend validation | 112 tests | `cd backend && venv/Scripts/python scripts/test_all.py` |
| Frontend E2E (Playwright) | 26 tests | `cd frontend && npx playwright test e2e/full-suite.spec.ts` |
| SQL / routing / RAG-vs-truth evals | 57 LLM-driven cases | `eval_rag_vs_truth.py`, `eval_routing.py`, `eval_sql_breakdown.py` |
| Document-QA audit | 136 LLM-driven cases | see `backend/scripts/DOC_QA_PLAYBOOK.md` |

Both eval suites were audited to **two consecutive 100% runs** (57/57 ×2 on the load-schedule matrix, 136/136 ×2 on document QA). Playbooks for running and resuming them: [`EVAL_PLAYBOOK.md`](./backend/scripts/EVAL_PLAYBOOK.md) and [`DOC_QA_PLAYBOOK.md`](./backend/scripts/DOC_QA_PLAYBOOK.md).

## Getting Started

1. **Supabase** — create a project, then run the migrations in `backend/migrations/` in order (SQL Editor)
2. **Backend**
   ```bash
   cd backend
   python -m venv venv
   venv/Scripts/pip install -r requirements.txt   # Windows; use venv/bin/pip on macOS/Linux
   ```
   Create `backend/.env` (copy `backend/.env.example` and fill it in):
   ```
   SUPABASE_URL=...
   SUPABASE_ANON_KEY=...
   SUPABASE_SERVICE_ROLE_KEY=...
   GEMINI_API_KEY=...
   LANGSMITH_API_KEY=...             # optional, for tracing
   LANGSMITH_PROJECT=...             # optional
   TEST_USER_ADMIN_PASSWORD=...      # optional, only needed to run the backend tests
   ```
   Tavily (web search) and Cohere (reranking) keys are **not** env vars — set them in the admin **Settings** UI.

   Run: `venv/Scripts/python -m uvicorn app.main:app --port 8001`
3. **Frontend**
   ```bash
   cd frontend
   npm install
   ```
   Create `frontend/.env.local` (copy `frontend/.env.example` and fill it in):
   ```
   VITE_SUPABASE_URL=...
   VITE_SUPABASE_ANON_KEY=...
   ```
   Run: `npm run dev` → open http://localhost:5173
4. Sign up, promote your user to admin (`profiles.is_admin = true` in Supabase), configure models/tools in **Settings**, upload documents, and start asking questions.

## Project History

Built in public, checkpoint by checkpoint — each tag is a working snapshot you can check out:

| Tag | Milestone |
|---|---|
| `Module-2-With-Settings` … `Module-8-Sub-Agents` | Course modules: BYO retrieval, record manager, metadata + auto-filter, Docling, hybrid search, additional tools, sub-agents |
| `Episode-1-Complete` | End of Episode 1 (Modules 1–8) |
| `v1.0` | File explorer milestone — folders, two-scope RLS, 5 exploration tools, explorer sub-agent |
| `v1.1` | Eval hardening — 57-case SQL/routing matrix + 136-case doc-QA audit, both at two consecutive 100% runs |

## Docs

- [PRD.md](./PRD.md) — original product requirements (the 8 course modules)
- [CLAUDE.md](./CLAUDE.md) — rules and context for Claude Code
- [PROGRESS.md](./PROGRESS.md) — detailed build log, module by module, with every fix
- `backend/scripts/EVAL_PLAYBOOK.md` / `DOC_QA_PLAYBOOK.md` — eval harness runbooks

## Credits & Community

This project follows [The AI Automators' Claude Code Agentic RAG Masterclass](https://www.youtube.com/watch?v=xgPWCuqLoek) — a course where you don't write the code, you direct Claude Code and course-correct. Join builders creating production-grade AI systems at [The AI Automators community](https://www.theaiautomators.com/).
