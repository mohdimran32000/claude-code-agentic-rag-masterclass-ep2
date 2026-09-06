import contextvars
import json
import logging
import re
from itertools import zip_longest
from typing import Generator, Optional, List

# The user question driving the current stream_response call — consumed by
# _windowed_excerpt when trimming retrieved chunks for the answer context.
# ContextVar so concurrent async requests can't bleed into each other.
_current_question: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "current_question", default="")

from google import genai
from google.genai import types
from langsmith import traceable

from app.services.settings import (
    get_llm_api_key, get_llm_model, get_metadata_schema,
    get_text_to_sql_enabled, get_web_search_enabled,
)

logger = logging.getLogger(__name__)

_client_cache = {"key": None, "client": None}


def _get_client() -> genai.Client:
    key = get_llm_api_key()
    if not key:
        raise ValueError("No Gemini API key configured. Set GEMINI_API_KEY in .env or configure it in Admin Settings.")
    if _client_cache["key"] != key or _client_cache["client"] is None:
        # 3-minute per-request timeout: without one, a wedged HTTP connection
        # hangs generate_content forever (observed during the doc-QA audit —
        # eval runs froze mid-case with zero CPU in the API call). A timeout
        # surfaces as an exception that caller-side retries can handle.
        _client_cache["client"] = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=180_000),
        )
        _client_cache["key"] = key
    return _client_cache["client"]


SYSTEM_PROMPT_NO_DOCS = "You are a helpful assistant. Answer the user's questions clearly and concisely."

OUTPUT_FORMAT_RULES = """
OUTPUT FORMAT RULES (strict):
- Never output raw HTML in your answer. Tags like <table>, <tr>, <td>, <th>, <br>, <span>, <div> are forbidden. If the source excerpts contain HTML, extract the data into clean markdown.
- For tabular source data, prefer a concise markdown bulleted list unless the user asked for a table, list, breakdown, or Excel-style output. When the user DOES ask for a list/breakdown/table/"Excel format", render EVERY relevant row as a clean markdown table — never truncate or stop partway. Pick the columns that answer the question (e.g. board, circuit, area/location, quantity). MANDATORY: when a quantity/points column is present, the table's last row MUST be a Total row summing it, and the sentence introducing or closing the table MUST state that total explicitly.
- Never reveal internal notes from tool results. Lines like "SQL: `...`" and blocks starting with "IMPORTANT (for interpreting these results)" are instructions for YOU — apply them silently, never quote or mention them in the answer.
- Data cells sometimes contain long data-entry/verification notes (e.g. "blank as printed - verified against image...", "SL NO printed twice on this page..."). Present only the meaningful value (e.g. "FCU") and drop the note.
- EXCEPTION: if a note records a substantive correction to a value (struck out, superseded, handwritten replacement, revised by the authority), do surface it briefly — state the current value and mention the original (e.g. "Max Demand: 1156.36 kW (corrected by DEWA from the printed 1120.40)"). Never present a superseded value as current.
- When excerpts contain BOTH project/site-specific records (asset registers, schedules, commissioning sheets, warranty letters) AND generic manufacturer literature (datasheets, marketing copy), answer from the project-specific records — they state what was actually installed WHERE and FOR WHAT. Use generic literature only for details the project records lack.
- For questions about warranties, maintenance terms, or service intervals: lead with the CONCRETE values found in the excerpts (duration, start/end dates, frequency, who provides it) before any terms and conditions. If a warranty letter states a period (e.g. "12 months from ..."), that period IS the answer — never reply with only the claim conditions.
- Never present an equipment RATING (W, kW, kVA, A) as energy CONSUMPTION (kWh), and never estimate consumption/cost/runtime from ratings — if consumption is asked and only ratings exist, say the records do not state it.
- When the user asks for the answer "as a table" or "in a table", the answer MUST contain a markdown table — a bulleted list is not acceptable.
- Never paste, echo, or reproduce source excerpts verbatim. Always synthesize the answer in your own words.
- When the user asks you to explain or simplify a figure from earlier in the conversation ("explain it in simple terms"), ALWAYS restate that figure explicitly in the explanation — an explanation that never names the value it explains is incomplete.
- Only state facts that appear in the provided excerpts/results. If they don't contain the asked-for specifics (locations, values, dates, names), say the records don't show them — NEVER construct plausible-looking specifics.
- Keep answers focused on what was asked. If a source has extra detail, leave it out."""


def _build_system_prompt(has_documents: bool, has_structured_data: bool, web_search_enabled: bool, structured_tables=None) -> str:
    """Build system prompt dynamically based on which tools are available."""
    if not has_documents and not has_structured_data and not web_search_enabled:
        return SYSTEM_PROMPT_NO_DOCS

    parts = ["You are a helpful assistant with access to the following tools:"]

    if has_documents:
        parts.append("- analyze_document: Summarize, review, or analyze a specific document in depth. Loads the FULL document. MUST be used for any summarization request.")
        parts.append("- search_documents: Find specific facts or snippets across documents. Only returns a few excerpts — NOT for summarization.")
        parts.append("- explore_knowledge_base: Open-ended exploration that spawns a sub-agent which iteratively uses tree/glob/grep/list_files/read_document over up to 8 turns and returns a compact summary. Use when the user asks 'find everything about Y' or 'what does the KB say about Z' and the answer requires multi-step exploration to even find what to read. NOT for single-fact lookups — those go to search_documents.")
    if has_structured_data:
        sql_line = "- query_structured_data: Query tabular data (from CSV/XLSX files) using SQL. Use this for quantitative questions (totals, averages, counts, comparisons) and for looking up specific values — but ONLY when the subject of the question lives in the available tables listed below."
        schema_summary = _format_structured_tables(structured_tables)
        if schema_summary:
            sql_line += f" Available tables: {schema_summary}"
        parts.append(sql_line)
    if web_search_enabled:
        parts.append("- web_search: Search the web for information not found in the user's documents.")

    parts.append("")
    parts.append("TOOL SELECTION RULES (follow strictly):")
    if has_documents:
        parts.append("- ALWAYS use analyze_document when the user asks to summarize, analyze, review, or extract key findings from a specific document by name. This tool loads the FULL document — search_documents only returns a few snippets and cannot produce a real summary.")
        parts.append("- Use search_documents ONLY for finding specific facts, quotes, or snippets across multiple documents (e.g. 'what does the contract say about payment terms?').")
        # SEARCH-03 NEW: self-scope hint — teach the LLM to pass folder_path / scope
        # on search_documents when the user's question is clearly narrowed to a
        # folder or a scope (private vs shared knowledge base).
        parts.append(
            "- When the user's question is clearly scoped to a folder, pass `folder_path` "
            "to search_documents to narrow the search (e.g. 'in /projects/2026'). When "
            "the question is about admin-curated shared content vs. the user's private "
            "docs, pass `scope='global'` or `scope='user'`. Otherwise leave both unset."
        )
        # Phase 4 NEW: precision-tools overview — introduce the 5 exploration tools
        # so the LLM knows when to prefer them over search_documents.
        parts.append(
            "- For codebase-style precision: use `tree` to see the folder structure, "
            "`glob` to find files by name pattern (e.g. '**/*.pdf'), `grep` to search "
            "inside document text by regex, `list_files` to see one folder's contents, "
            "and `read_document` to read specific lines of a doc. Prefer these over "
            "search_documents when the user asks 'where is X' or 'show me all PDFs in "
            "/projects'."
        )
        # Phase 5 NEW: explore_knowledge_base disambiguation — distinguishes
        # the three closest tools (analyze_document = specific named doc;
        # search_documents = single-shot snippet retrieval; explore_knowledge_base
        # = multi-step iterative exploration that delegates to the precision tools).
        parts.append(
            "- For OPEN-ENDED exploration that needs multiple steps, use "
            "`explore_knowledge_base`. It runs a sub-agent that calls "
            "tree/glob/grep/list_files/read_document iteratively and returns a "
            "compact summary. Use `analyze_document` for a specific NAMED "
            "document, `search_documents` for one-shot snippet retrieval, and "
            "`explore_knowledge_base` when the user's question requires multiple "
            "steps to even locate what to read. A SINGLE-FACT question (what "
            "brand/model, where is a device, how often is X serviced) is NOT "
            "exploration — always use search_documents for those; it is faster "
            "and more reliable than the exploration sub-agent."
        )
        # Phase 4 NEW: scope disambiguation in citations (TOOL-07 invariant /
        # Pitfall 11 mitigation — every tool result row carries a scope field).
        parts.append(
            "- Tool results carry a 'scope' field on every row. When citing a result, "
            "mention whether it came from the user's private docs (scope='user') or the "
            "shared knowledge base (scope='global'). Don't conflate the two."
        )
    if has_structured_data:
        parts.append(
            "- For questions about tabular data, use query_structured_data — this includes "
            "quantitative analysis (numbers, totals, averages) AND single-value lookups when "
            "the entity the user names appears in one of the available tables (e.g. 'what is "
            "the rating/load of X' where X is a row in a table). Only fall back to "
            "search_documents for tabular entities when SQL returns nothing."
        )
        parts.append(
            "- Route by the SUBJECT of the question, not its shape: a count/total/spec "
            "question about equipment or facts that are NOT rows or columns of the "
            "available tables (e.g. devices, systems, warranties, procedures described "
            "in uploaded manuals) must use the document tools (search_documents), NOT "
            "query_structured_data. 'How many X …' only goes to SQL when X is something "
            "the tables actually store."
        )
        parts.append(
            "- When the question is anchored to a NAMED SYSTEM or its documentation "
            "(e.g. 'how many X does the <system> control/monitor/serve', 'in the "
            "<system> system', 'per the manual'), use the document tools even if the "
            "counted item also appears in a table column — the tables describe the "
            "items themselves, not which system controls them or how it behaves."
        )
        parts.append(
            "- The tables hold quantities, loads, and ratings — NOT manufacturers, "
            "models, brands, or part numbers. Questions asking WHAT make/model/brand/"
            "unit/device is installed (even inside equipment the tables list, like "
            "panels) go to the document tools."
        )
    if web_search_enabled:
        parts.append("- For current events or information not in the user's documents, use web_search.")
    parts.append("- For casual greetings or questions clearly unrelated to any tool, respond directly without calling a tool.")
    parts.append("- Only call ONE tool per turn.")
    # Answers produced WITHOUT a tool round-trip (follow-ups answered from
    # conversation history) stream this same prompt's response directly, so the
    # format rules must live here too — not only in the tool-result final call.
    parts.append(OUTPUT_FORMAT_RULES)

    return "\n".join(parts)


def _build_search_tool() -> types.Tool:
    """Build the search_documents tool definition dynamically from the metadata schema."""
    schema = get_metadata_schema()

    filter_properties = {}
    for field in schema:
        field_type = field.get("type", "text")
        if field_type == "text":
            filter_properties[field["name"]] = types.Schema(
                type="STRING", description=field.get("description", ""),
            )
        elif field_type == "list":
            filter_properties[field["name"]] = types.Schema(
                type="ARRAY", items=types.Schema(type="STRING"),
                description=field.get("description", ""),
            )
        elif field_type == "boolean":
            filter_properties[field["name"]] = types.Schema(
                type="BOOLEAN", description=field.get("description", ""),
            )
        elif field_type == "number":
            filter_properties[field["name"]] = types.Schema(
                type="NUMBER", description=field.get("description", ""),
            )
        elif field_type == "date":
            filter_properties[field["name"]] = types.Schema(
                type="STRING",
                description=f"{field.get('description', '')} (YYYY-MM-DD format)",
            )

    return types.FunctionDeclaration(
        name="search_documents",
        description=(
            "Find specific facts, quotes, or snippets across the user's documents. "
            "Returns only a few matching excerpts — NOT suitable for summarizing or analyzing a whole document. "
            "Do NOT use this for summarization requests — use analyze_document instead."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(
                    type="STRING",
                    description="A SHORT, focused search query (under 50 words). Extract specific identifiers (codes, model numbers, names) and the core question. Do NOT include email headers, greetings, or background context.",
                ),
                **filter_properties,
                # SEARCH-01 NEW: optional narrowing args. NULL defaults preserve
                # pre-Phase-4 behavior bit-for-bit when omitted (Migration 020's
                # match_folder_path/match_scope DEFAULT NULL no-op the predicates).
                "folder_path": types.Schema(
                    type="STRING",
                    description=(
                        "OPTIONAL prefix filter. Only documents under this canonical "
                        "path are searched. Use when the user's question is clearly "
                        "scoped to a specific area of the knowledge base (e.g., "
                        "'in /projects/2026'). Default: null (no narrowing). "
                        "Path must start with '/'. The 5 precision tools (tree, glob, "
                        "grep, list_files, read_document) accept the same path shape."
                    ),
                ),
                "scope": types.Schema(
                    type="STRING",
                    enum=["user", "global", "both"],
                    description=(
                        "OPTIONAL. Restrict search to user-private docs ('user'), "
                        "global shared docs ('global'), or both ('both'). When omitted "
                        "(default), no narrowing is applied — the result includes "
                        "whatever RLS allows the caller to see (private + global)."
                    ),
                ),
            },
            required=["query"],
        ),
    )


def _format_structured_tables(structured_tables) -> str:
    """One-line schema summary per table: name(col1, col2, ...) — capped for prompt size.

    Cap raised 20 -> 40 (2026-08-19): the Heriot Watt corpus alone reaches 28 tables, and
    a table missing from this summary is invisible to the ROUTER — it may never think to
    call query_structured_data for that topic. execute_sql_query itself has always seen
    every table, so this only widens the router's awareness, it does not change reach.
    """
    if not structured_tables:
        return ""
    lines = []
    for t in structured_tables[:40]:
        cols = t.get("columns") or []
        col_str = ", ".join(str(c) for c in cols[:15])
        if len(cols) > 15:
            col_str += ", ..."
        lines.append(f"{t.get('table_name', '?')}({col_str})")
    return "; ".join(lines)


def _build_sql_tool(structured_tables=None) -> types.FunctionDeclaration:
    """Build the query_structured_data tool definition."""
    description = (
        "Query the user's tabular data (from uploaded CSV/XLSX files) using SQL. "
        "Use this for totals, counts, averages, comparisons, AND for looking up any "
        "specific value stored in these tables — even a single cell (e.g. the rating, "
        "load, or attribute of one named row). Prefer this over search_documents "
        "whenever the entity the user asks about appears in a table below. Do NOT "
        "use it when the subject of the question is not stored in these tables — "
        "counts/specs of equipment described only in uploaded documents belong to "
        "search_documents."
    )
    schema_summary = _format_structured_tables(structured_tables)
    if schema_summary:
        description += f" Available tables: {schema_summary}"
    return types.FunctionDeclaration(
        name="query_structured_data",
        description=description,
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "question": types.Schema(
                    type="STRING",
                    description=(
                        "The user's question, passed through VERBATIM. Only resolve "
                        "pronouns/references from conversation context (e.g. 'give a "
                        "breakdown of it' -> 'give a breakdown of the FCUs on the 4th "
                        "floor of Block B'). Do NOT rephrase domain terms, do NOT name "
                        "tables or columns, and do NOT reinterpret what is being asked — "
                        "the SQL engine sees the full schema and maps the question itself; "
                        "a paraphrase that guesses the wrong table/column produces a "
                        "confidently wrong answer."
                    ),
                ),
            },
            required=["question"],
        ),
    )


def _build_web_search_tool() -> types.FunctionDeclaration:
    """Build the web_search tool definition."""
    return types.FunctionDeclaration(
        name="web_search",
        description=(
            "Search the web for information not found in the user's uploaded documents. "
            "Use this for current events, external facts, or when document search returns no relevant results."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(
                    type="STRING",
                    description="The search query to find relevant web results",
                ),
            },
            required=["query"],
        ),
    )


def _build_analyze_tool() -> types.FunctionDeclaration:
    """Build the analyze_document tool definition for deep single-document analysis."""
    return types.FunctionDeclaration(
        name="analyze_document",
        description=(
            "REQUIRED for summarizing, reviewing, or analyzing a document. "
            "Loads the FULL document content for comprehensive analysis. "
            "Use this whenever the user says 'summarize', 'analyze', 'review', or 'explain' a document."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "document_name": types.Schema(
                    type="STRING",
                    description="Name of the document to analyze",
                ),
                "question": types.Schema(
                    type="STRING",
                    description="What to analyze about this document",
                ),
            },
            required=["document_name", "question"],
        ),
    )


def _build_list_files_tool() -> "types.FunctionDeclaration":
    """Phase 4 / TOOL-04: list_files tool definition.

    Single-folder one-level listing. Returns folders + files at the given path,
    folders-then-files alphabetical. Use when the user asks 'what's in <folder>?'
    — distinct from `tree` (multi-level) and `glob` (pattern matching).
    """
    from google.genai import types
    return types.FunctionDeclaration(
        name="list_files",
        description=(
            "List the immediate folders and files under a single path (one level deep). "
            "Use when the user asks 'what's in <folder>?' or 'show me the files in <folder>'. "
            "Returns folders first (alpha-sorted), then documents (alpha-sorted). "
            "Each row carries 'scope' ('user' for private, 'global' for shared). "
            "For multi-level views use `tree`; for name patterns use `glob`."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "path": types.Schema(
                    type="STRING",
                    description="Canonical folder path (must start with '/'; '/' for root).",
                ),
                "scope": types.Schema(
                    type="STRING",
                    enum=["user", "global", "both"],
                    description="'user' for private docs, 'global' for shared, 'both' (default) for union.",
                ),
            },
            required=[],
        ),
    )


def _build_tree_tool() -> "types.FunctionDeclaration":
    """Phase 4 / TOOL-01: tree tool definition.

    Multi-level folder structure with depth and entry-budget caps. Use when the
    user wants an overview of the knowledge base shape (e.g., 'show me the
    folder structure'). For a single-level listing use `list_files`; for name
    patterns use `glob`.
    """
    from google.genai import types
    return types.FunctionDeclaration(
        name="tree",
        description=(
            "Show a nested folder + document tree at a given path with a configurable "
            "depth limit. Returns folders and docs grouped by parent, with "
            "'[N more folders, M more docs]' summaries when the depth or 500-entry "
            "budget is hit. Each row carries 'scope' ('user' for private, 'global' "
            "for shared). Use when the user asks for an overview ('show me the structure'); "
            "for a single folder one level deep use `list_files`; for name patterns use `glob`."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "path": types.Schema(
                    type="STRING",
                    description="Canonical folder path (must start with '/'; '/' for root).",
                ),
                "max_depth": types.Schema(
                    type="INTEGER",
                    description="Recursion depth (1-4; default 2; server-capped at 4).",
                ),
                "scope": types.Schema(
                    type="STRING",
                    enum=["user", "global", "both"],
                    description="'user' for private docs, 'global' for shared, 'both' (default) for union.",
                ),
            },
            required=[],
        ),
    )


def _build_glob_tool() -> "types.FunctionDeclaration":
    """Phase 4 / TOOL-02: glob tool definition.

    File-name and folder-path pattern matching with `**` (any depth) and `*`
    (single segment) semantics. Use when the user asks 'find all PDFs' or
    'show me everything under /projects/2026/floor-plans'.
    """
    from google.genai import types
    return types.FunctionDeclaration(
        name="glob",
        description=(
            "Find files or folders by name pattern. Supports `**` (any depth) and "
            "`*` (single segment). Examples: '**/*.pdf' (all PDFs anywhere), "
            "'projects/**/floor-plans/*' (everything in any 'floor-plans' subfolder). "
            "`type` selects 'file' (docs only), 'folder' (folders only), or 'both' (default). "
            "Each row carries 'scope' ('user' for private, 'global' for shared). "
            "Use when looking by name; for tree shape use `tree`; for content search use `grep`."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "pattern": types.Schema(
                    type="STRING",
                    description="Glob pattern with `**`/`*` semantics. 1-200 chars.",
                ),
                "path": types.Schema(
                    type="STRING",
                    description="Restrict matching to this prefix (canonical path; '/' for root).",
                ),
                "type": types.Schema(
                    type="STRING",
                    enum=["file", "folder", "both"],
                    description="'file' for docs only, 'folder' for folders only, 'both' (default).",
                ),
                "scope": types.Schema(
                    type="STRING",
                    enum=["user", "global", "both"],
                    description="'user' / 'global' / 'both' (default).",
                ),
            },
            required=["pattern"],
        ),
    )


def _build_read_document_tool() -> "types.FunctionDeclaration":
    """Phase 4 / TOOL-05: read_document tool definition.

    Line-numbered slice of a document's content. Use when the user wants to
    see the literal text of a known document. For pattern-search across many
    docs use `grep`; for full-document analysis use `analyze_document`.
    """
    from google.genai import types
    return types.FunctionDeclaration(
        name="read_document",
        description=(
            "Read a line-numbered slice of one document's content. Returns lines in "
            "arrow form ('123→content of line 123'). Specify either `document_id` "
            "OR `path` (e.g. '/projects/readme.md'). Default reads lines 1-2000; "
            "hard cap 5000 lines. Use when you need the literal text of a known "
            "document; for content search across many docs use `grep`."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "document_id": types.Schema(
                    type="STRING",
                    description="UUID of the document. Either this OR `path` is required.",
                ),
                "path": types.Schema(
                    type="STRING",
                    description="Folder + file_name combo, e.g. '/projects/readme.md'. "
                                "Either this OR `document_id` is required.",
                ),
                "offset": types.Schema(
                    type="INTEGER",
                    description="1-based line number to START at (default 1).",
                ),
                "limit": types.Schema(
                    type="INTEGER",
                    description="Lines to return (default 2000; hard cap 5000).",
                ),
            },
            required=[],
        ),
    )


def _build_grep_tool() -> "types.FunctionDeclaration":
    """Phase 4 / TOOL-03: grep tool definition.

    Regex search across document content. Use when the user wants to find a
    phrase, term, or pattern across many documents (e.g., 'find all docs that
    mention panel MDB-C-G3'). For navigation by name use `glob`; for reading
    a known doc use `read_document`.
    """
    from google.genai import types
    return types.FunctionDeclaration(
        name="grep",
        description=(
            "Regex search across document text. Returns matching lines with +/-A/B "
            "lines of context (default +/-2). `output_mode` selects 'content' (default; "
            "lines + context), 'files_with_matches' (just doc list), or 'count' "
            "(per-doc match count). Each hit carries 'scope' ('user' for private, "
            "'global' for shared). Pathological regex (e.g., `(.*)+`) is rejected. "
            "Documents that haven't been re-indexed yet appear with status='pending_reindex' "
            "rather than being silently skipped. Use for content search; use `glob` for "
            "name patterns; use `read_document` to read a known doc."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "pattern": types.Schema(
                    type="STRING",
                    description="Postgres-flavor regex (1-500 chars).",
                ),
                "path": types.Schema(
                    type="STRING",
                    description="Restrict search to this folder prefix (default '/').",
                ),
                "case_insensitive": types.Schema(
                    type="BOOLEAN",
                    description="Default true.",
                ),
                "multiline": types.Schema(
                    type="BOOLEAN",
                    description="Default false (rarely needed).",
                ),
                "output_mode": types.Schema(
                    type="STRING",
                    enum=["content", "files_with_matches", "count"],
                    description="Default 'content'.",
                ),
                "A": types.Schema(
                    type="INTEGER",
                    description="Lines AFTER each match (0-10; default 2).",
                ),
                "B": types.Schema(
                    type="INTEGER",
                    description="Lines BEFORE each match (0-10; default 2).",
                ),
                "C": types.Schema(
                    type="INTEGER",
                    description="If set, overrides both A and B.",
                ),
                "scope": types.Schema(
                    type="STRING",
                    enum=["user", "global", "both"],
                    description="'user' / 'global' / 'both' (default).",
                ),
            },
            required=["pattern"],
        ),
    )


def _build_explore_knowledge_base_tool() -> "types.FunctionDeclaration":
    """Build the explore_knowledge_base tool definition for open-ended exploration.

    Use when the user's question is open-ended ('where are X', 'what's in the KB
    about Y', 'find me all docs related to Z') and answering requires multiple
    steps. Spawns an isolated sub-agent that iteratively calls
    tree/glob/grep/list_files/read_document for up to 8 turns then returns a
    compact summary. Distinct from analyze_document (which targets a SPECIFIC
    named document). Recursive sub-agents forbidden — Explorer cannot call
    analyze_document or itself (EXPLORER-03 setup-time assert in sub_agent.py).
    """
    return types.FunctionDeclaration(
        name="explore_knowledge_base",
        description=(
            "REQUIRED for OPEN-ENDED exploration of the user's knowledge base. "
            "Use when the user asks 'where is X', 'find me everything about Y', or "
            "'what does the KB say about Z' and the answer requires multiple steps. "
            "Spawns an exploration sub-agent that uses tree, glob, grep, list_files, "
            "read_document for up to 8 turns then returns a compact summary. "
            "Distinct from analyze_document — that tool is for a specific named document. "
            "Distinct from search_documents — that tool returns raw snippets in one shot."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(
                    type="STRING",
                    description=(
                        "The open-ended exploration question. Pass the user's question "
                        "verbatim or a slightly normalized version. The sub-agent has its "
                        "own system prompt and will plan the tool sequence."
                    ),
                ),
            },
            required=["query"],
        ),
    )


def _sanitize_keyword_query(q: str, semantics: str = "and") -> str:
    """Build a websearch_to_tsquery string from the user's query.

    Always strips operators first: a space-prefixed hyphen (e.g. `MDB -C-G3`
    from email formatting) is the NOT operator in websearch syntax, silently
    excluding the very chunks the user wants.

    semantics="and" (websearch default): every term must match — precise but
    brittle for full-sentence questions, which may match zero or one chunk.
    semantics="or": any term matches, ts_rank_cd still ranks multi-term chunks
    higher — the recall-recovery form used when the AND pass under-fills.
    """
    import re
    q = q.replace("-", " ").replace('"', " ")
    q = re.sub(r"\bor\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip()
    if semantics == "or":
        tokens = [t for t in q.split(" ") if t]
        # Unit-suffixed tokens tokenize differently in source text ("8kVA" is
        # one lexeme, "8 KVA" is two) — add split variants so either form hits.
        for t in list(tokens):
            parts = re.findall(r"\d+|[A-Za-z]+", t)
            if len(parts) > 1:
                tokens += [p for p in parts if len(p) > 1 or p.isdigit()]
        return " or ".join(dict.fromkeys(tokens))
    return q


def retrieve_chunks(
    query: str,
    user_id: str,
    supabase_client,
    top_k: int = 5,
    metadata_filter: Optional[dict] = None,
    folder_path: Optional[str] = None,
    scope: Optional[str] = None,
) -> List[dict]:
    """Embed query and search via hybrid (vector + keyword RRF) or vector-only RPC.
    Returns list of dicts with keys: content, document_id, file_name.

    SEARCH-01/02: `folder_path` and `scope` are optional narrowing args forwarded to
    Migration 020's `match_folder_path` and `match_scope` RPC params. Both default
    to None — Migration 020's NULL defaults preserve Phase 1/2/3 behavior bit-for-bit
    when omitted.
    """
    from app.services.ingestion import TASK_TYPE_QUERY, embed_text
    from app.services.settings import get_hybrid_search_enabled, get_reranking_enabled

    # RETRIEVAL_QUERY, not the DOCUMENT default: this is the asking side of the pair.
    # Must stay matched with how the corpus was embedded (ingestion.TASK_TYPE_DOCUMENT).
    query_embedding = embed_text(query, TASK_TYPE_QUERY)
    hybrid = get_hybrid_search_enabled()
    reranking = get_reranking_enabled()

    if hybrid:
        fetch_count = top_k * 4 if reranking else top_k

        def _hybrid_call(query_text):
            return supabase_client.rpc("match_document_chunks_hybrid", {
                "query_embedding": query_embedding,
                "query_text": query_text,
                "match_user_id": user_id,
                "match_count": fetch_count,
                "metadata_filter": json.dumps(metadata_filter) if metadata_filter else None,
                # SEARCH-02: forward optional narrowing args (NULL = no narrowing).
                "match_folder_path": folder_path,
                "match_scope": scope,
            }).execute().data or []

        # Keyword ladder: AND-semantics first (precise). Full-sentence queries
        # often AND-match almost nothing, so when the precise pass under-fills,
        # top it up with OR-semantics results (any term, ranked by ts_rank_cd) —
        # AND hits keep their positions, OR hits fill the remaining slots.
        rows = _hybrid_call(_sanitize_keyword_query(query))
        if len(rows) < fetch_count:
            seen_ids = {r["id"] for r in rows}
            rows += [r for r in _hybrid_call(_sanitize_keyword_query(query, semantics="or"))
                     if r["id"] not in seen_ids]
            rows = rows[:fetch_count]
    else:
        result = supabase_client.rpc("match_document_chunks_with_filters", {
            "query_embedding": query_embedding,
            "match_user_id": user_id,
            "match_count": top_k,
            "metadata_filter": json.dumps(metadata_filter) if metadata_filter else None,
            # SEARCH-02: forward optional narrowing args (NULL = no narrowing).
            "match_folder_path": folder_path,
            "match_scope": scope,
        }).execute()
        rows = result.data or []

    # Fetch document names for source attribution
    doc_ids = list(set(row["document_id"] for row in rows))
    doc_names = {}
    if doc_ids:
        docs = supabase_client.table("documents").select("id, file_name").in_("id", doc_ids).execute()
        doc_names = {d["id"]: d["file_name"] for d in (docs.data or [])}

    chunks = [
        {"content": row["content"], "document_id": row["document_id"], "file_name": doc_names.get(row["document_id"], "Unknown")}
        for row in rows
    ]

    if hybrid and reranking and len(chunks) > top_k:
        from app.services.reranker import rerank_chunks
        content_strings = [c["content"] for c in chunks]
        reranked_contents = rerank_chunks(query, content_strings, top_k)
        content_to_chunk = {c["content"]: c for c in chunks}
        chunks = [content_to_chunk[rc] for rc in reranked_contents if rc in content_to_chunk]

    return chunks[:top_k]


def _windowed_excerpt(query: str, content: str, head: int = 900,
                      win: int = 1300, max_windows: int = 2) -> str:
    """Trim a retrieved chunk to its query-relevant regions for the answer
    context. Raw OCR chunks run to 7k+ chars; lite answer models demonstrably
    lose facts buried mid-chunk (e.g. a warranty period at offset ~5k). Keeps
    the head plus windows around the FIRST and LAST query-term hits beyond it.
    """
    # Only trim genuinely huge chunks: the answer context cap (60k) fits ~12
    # mid-sized chunks whole, and trimming a 3.5k chunk once cut the very
    # letter the question asked about (it sat in the final 400 chars). The
    # lite answer model only demonstrably loses facts in 5k+ OCR blobs.
    if len(content) <= 4000:
        return content
    low = content.lower()
    # Longest terms first — a rarity proxy, so 'warranty'/'switchgear' windows
    # beat filler like 'have'/'what'. One window per distinct term's FIRST hit
    # beyond the head: first+last-only windowing leaves a gap in the middle
    # where the actual fact may sit.
    terms = sorted({t for t in re.findall(r"[\w]+", query.lower()) if len(t) >= 4},
                   key=len, reverse=True)
    out, covered = [content[:head]], []
    for t in terms:
        if len(covered) >= max_windows:
            break
        pos = low.find(t, head)
        if pos == -1:
            continue
        start = max(head, pos - 250)
        if any(abs(start - c) < win for c in covered):
            continue
        covered.append(start)
        out.append(content[start:start + win])
    if not covered:
        return content[:head + win]
    # Query-matched windows FIRST: the head of an OCR chunk is often table
    # junk, and the answer model demonstrably anchors on leading text — a
    # decisive sentence buried mid-excerpt gets glossed over.
    return " […] ".join(out[1:] + [out[0][:400]])


def _resolve_document(supabase_client, user_id: str, doc_name: str):
    """Fuzzy-resolve a user-supplied document name to a documents row.

    Pass 1: whole-phrase ilike (existing behavior). Pass 2: the phrase rarely
    matches verbatim ("the LV switchgear manual" vs the long real filename),
    so AND-match each significant token instead — generic filler words like
    "manual"/"document" are dropped.
    """
    q = supabase_client.table("documents").select("id, file_name") \
        .eq("user_id", user_id).ilike("file_name", f"%{doc_name}%") \
        .order("created_at", desc=True).limit(1).execute()
    if q.data:
        return q
    # Token-overlap ranking in Python: the request often carries request verbs
    # and filler ("summarize the LV switchgear manual for me") that no filename
    # contains — rank files by how many meaningful tokens they share instead.
    filler = {"manual", "manuals", "document", "documents", "doc", "file",
              "files", "the", "for", "and", "o&m", "om", "report", "guide",
              "summarize", "summarise", "summary", "analyze", "analyse",
              "review", "explain", "overview", "please", "can", "you", "me",
              "give", "show", "full", "whole", "entire", "this", "that", "of"}
    tokens = {t for t in re.findall(r"[\w&]+", doc_name.lower())
              if len(t) > 1 and t not in filler}
    if not tokens:
        return q
    all_docs = supabase_client.table("documents").select("id, file_name") \
        .eq("user_id", user_id).limit(200).execute()
    best, best_score = None, 0
    for d in (all_docs.data or []):
        fn_tokens = set(re.findall(r"[\w&]+", d["file_name"].lower()))
        # Substring-aware: "zip" must match the filename token "ziptaps",
        # otherwise a generic shared word ("water") wins the wrong file.
        score = sum(
            1 for t in tokens
            if any(t == ft or (len(t) >= 3 and t in ft) or (len(ft) >= 3 and ft in t)
                   for ft in fn_tokens)
        )
        if score > best_score:
            best, best_score = d, score
    q.data = [best] if best else []
    return q


def _sql_result_is_empty(result_text: str) -> bool:
    """True when a query_structured_data result carries no data: either the
    explicit no-results message, or a markdown table whose data cells are all
    NULL (rendered as empty) — e.g. SUM() over zero matching rows. A 0 value
    is NOT empty; zero can be a correct answer."""
    if result_text.startswith("Query returned no results"):
        return True
    table_lines = [l.strip() for l in result_text.splitlines() if l.strip().startswith("|")]
    if len(table_lines) < 3:
        return False
    # table_lines[0] is the header, [1] the |---|---| separator; the rest are
    # data rows — a row of NULLs renders as empty cells (`|  |`), so it must
    # NOT be filtered out here.
    cells = [c.strip() for r in table_lines[2:] for c in r.strip("|").split("|")]
    if bool(cells) and all(c == "" for c in cells):
        return True
    # A lone 0 (single row, single column) usually means COUNT/SUM matched no
    # rows — for a misrouted document question that's the "wrong tool" signal.
    # Multi-cell results with a 0 among real values are NOT empty.
    return len(cells) == 1 and cells[0] in ("0", "0.0")


@traceable(name="search_documents", run_type="tool")
def _execute_search_documents(
    search_query: str,
    metadata_filter: Optional[dict],
    user_id: Optional[str],
    supabase_client=None,
    folder_path: Optional[str] = None,
    scope: Optional[str] = None,
) -> List[dict]:
    """Execute the search_documents tool call — traced as a tool in LangSmith.

    SEARCH-01/02: `folder_path` and `scope` are optional LLM-driven narrowing args
    forwarded into `retrieve_chunks` → match_document_chunks_* RPCs. Both default
    to None which preserves pre-Phase-4 behavior bit-for-bit (Migration 020 NULL
    defaults short-circuit the predicates).
    """
    if not supabase_client or not user_id:
        return []
    try:
        return retrieve_chunks(
            query=search_query,
            user_id=user_id,
            supabase_client=supabase_client,
            # 12, not 10: with LLM reranking the borderline evidence chunk
            # (e.g. a warranty letter scoring ~0.5) drops below a top-10 cut
            # in a fraction of runs — two extra slots absorb the jitter.
            top_k=12,
            metadata_filter=metadata_filter,
            folder_path=folder_path,
            scope=scope,
        )
    except Exception as e:
        logger.warning(f"Tool retrieval failed: {e}")
        return []


@traceable(name="gemini_chat", run_type="llm")
def stream_response(
    messages: List[dict],
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    supabase_client=None,
    has_documents: bool = False,
    has_structured_data: bool = False,
    structured_tables: Optional[List[dict]] = None,
    manual_metadata_filter: Optional[dict] = None,
) -> Generator[tuple[str, str], None, None]:
    """
    Stream a Gemini chat completion with tool calling for document search.
    Uses Gemini's automatic function calling — the SDK handles the tool loop
    and thought_signature internally.
    """
    # Last TWO user turns: pronoun follow-ups ("where exactly are they
    # installed?") carry their subject in the previous turn — windowing and
    # excerpt trimming need those terms or the relevant passage gets cut.
    _user_turns = [m["content"] for m in messages if m["role"] == "user"][-2:]
    _current_question.set(" ".join(_user_turns))

    # If manual filter is set, pre-retrieve and use context injection (no tool calling)
    if manual_metadata_filter and has_documents and supabase_client and user_id:
        last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        chunks = retrieve_chunks(
            query=last_user_msg, user_id=user_id,
            supabase_client=supabase_client, top_k=10,
            metadata_filter=manual_metadata_filter,
        )
        context = "\n\n---\n\n".join(
            f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
        ) if chunks else "No relevant documents found."
        system_text = f"""You are a helpful assistant with access to the user's uploaded documents.
Use the provided document excerpts to answer questions accurately.
If the excerpts do not contain enough information to answer, say so and answer from general knowledge if applicable.
{OUTPUT_FORMAT_RULES}

Document excerpts:
{context}"""

        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

        client = _get_client()
        model = get_llm_model()
        response = client.models.generate_content_stream(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_text, temperature=0.2),
        )
        for chunk in response:
            if chunk.text:
                yield ("token", chunk.text)
        yield ("done", "")
        return

    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

    client = _get_client()
    model = get_llm_model()

    # Check which tools are enabled
    text_to_sql_enabled = get_text_to_sql_enabled() and has_structured_data
    web_search_enabled = get_web_search_enabled()

    # Build dynamic tool list — analyze_document listed first to reduce positional bias
    function_declarations = []
    if has_documents:
        try:
            function_declarations.append(_build_analyze_tool())
        except Exception as e:
            logger.warning(f"Failed to build analyze tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_search_tool())
        except Exception as e:
            logger.warning(f"Failed to build search tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_list_files_tool())
        except Exception as e:
            logger.warning(f"Failed to build list_files tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_tree_tool())
        except Exception as e:
            logger.warning(f"Failed to build tree tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_glob_tool())
        except Exception as e:
            logger.warning(f"Failed to build glob tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_read_document_tool())
        except Exception as e:
            logger.warning(f"Failed to build read_document tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_grep_tool())
        except Exception as e:
            logger.warning(f"Failed to build grep tool (non-fatal): {e}")
        try:
            function_declarations.append(_build_explore_knowledge_base_tool())
        except Exception as e:
            logger.warning(f"Failed to build explore_knowledge_base tool (non-fatal): {e}")
    if text_to_sql_enabled:
        try:
            function_declarations.append(_build_sql_tool(structured_tables))
        except Exception as e:
            logger.warning(f"Failed to build SQL tool (non-fatal): {e}")
    if web_search_enabled:
        try:
            function_declarations.append(_build_web_search_tool())
        except Exception as e:
            logger.warning(f"Failed to build web search tool (non-fatal): {e}")

    tools = None
    tool_config = None
    system_text = _build_system_prompt(has_documents, text_to_sql_enabled, web_search_enabled, structured_tables)
    if function_declarations:
        tools = [types.Tool(function_declarations=function_declarations)]
        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        )

    config = types.GenerateContentConfig(
        system_instruction=system_text,
        tools=tools,
        tool_config=tool_config,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # Deterministic tool routing — default temperature makes the
        # search-vs-SQL choice flaky for lookup questions
        temperature=0,
    )

    # Emit a thinking event when tools are available so the frontend can show status
    if function_declarations:
        tool_names = [fd.name for fd in function_declarations]
        yield ("tool_thinking", json.dumps({"available_tools": tool_names}))

    # Pre-check: detect summarization intent and force analyze_document
    # Gemini Flash has a strong bias toward search_documents even when analyze_document
    # is clearly the right tool. This deterministic override catches the obvious cases.
    forced_tool = None
    if has_documents and function_declarations:
        import re
        last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "").lower()
        summarize_pattern = re.search(r'\b(summarize|summarise|summary|analyze|analyse|review|explain|overview)\b', last_user_msg)
        # Quantitative questions often carry these words too ("what's the total
        # load for block B? explain in simple terms") — those are DATA questions
        # for the SQL tool, never document summarization. Detect and skip.
        # Plurals count ("review the loads"), and pronoun follow-ups ("explain
        # it in simple terms") carry their subject in earlier turns, so those
        # are scanned for the quantitative context too.
        quant_re = (r'\b(totals?|how many|counts?|sums?|averages?|loads?|demands?|ratings?'
                    r'|kw|kva|watts?|amps?|highest|lowest|capacity|breakdowns?)\b')
        quant_scan = last_user_msg
        if re.search(r'\b(it|that|this|them|those)\b', last_user_msg):
            recent = " ".join(str(m.get("content") or "") for m in messages[-4:])
            quant_scan = f"{last_user_msg}\n{recent.lower()}"
        quantitative_pattern = re.search(quant_re, quant_scan)
        if has_structured_data and quantitative_pattern:
            summarize_pattern = None
        if summarize_pattern:
            # Check if user references a specific document (not a generic question)
            generic_terms = {'document', 'documents', 'file', 'files', 'my'}
            words = set(re.findall(r'\b\w+\b', last_user_msg))
            non_generic = words - generic_terms - {summarize_pattern.group()} - {'the', 'a', 'an', 'for', 'me', 'can', 'you', 'please', 'of', 'this', 'that', 'it', 'to', 'in', 'do'}
            if non_generic:
                forced_tool = "analyze_document"
                logger.info(f"Forcing analyze_document — detected summarization intent with specific terms: {non_generic}")

    # Use non-streaming generate_content to handle the full tool call loop
    # Then stream the final response to the user
    from app.services.llm_usage import generate_with_usage

    response = generate_with_usage(
        client,
        model=model,
        contents=contents,
        config=config,
        name="chat_tool_loop",
    )

    # Handle malformed function calls — retry without tools using context injection
    if (response.candidates and
        response.candidates[0].finish_reason and
        response.candidates[0].finish_reason.name == "MALFORMED_FUNCTION_CALL"):
        logger.warning("Gemini returned MALFORMED_FUNCTION_CALL — falling back to pre-retrieval")
        last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        chunks = _execute_search_documents(
            search_query=last_user_msg, metadata_filter=None,
            user_id=user_id, supabase_client=supabase_client,
        )
        context = "\n\n---\n\n".join(
            f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
        ) if chunks else "No relevant documents found."
        fallback_system = f"""You are a helpful assistant with access to the user's uploaded documents.
Use the provided document excerpts to answer questions accurately.
If the excerpts do not contain enough information to answer, say so and answer from general knowledge if applicable.
{OUTPUT_FORMAT_RULES}

Document excerpts:
{context}"""
        response_fb = client.models.generate_content_stream(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=fallback_system, temperature=0.2),
        )
        for chunk in response_fb:
            if chunk.text:
                yield ("token", chunk.text)
        yield ("done", "")
        return

    # Check if we got a function call
    has_function_call = False
    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if part.function_call:
                has_function_call = True
                break

    if has_function_call:
        # Extract the function call
        fc = None
        for part in response.candidates[0].content.parts:
            if part.function_call:
                fc = part.function_call
                break

        args = dict(fc.args) if fc.args else {}
        tool_name = fc.name

        # Override tool choice if forced_tool is set and Gemini picked wrong.
        # (Quantitative questions never reach here — the detection above skips
        # them — so this only redirects genuine summarization requests.)
        if forced_tool and tool_name != forced_tool:
            logger.info(f"Overriding {tool_name} -> {forced_tool} (summarization intent detected)")
            last_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            # Keep the original search query as document_name hint — the ilike
            # fuzzy match in the analyze_document handler will resolve it
            tool_name = forced_tool
            original_query = args.get("query", "") or args.get("question", "") or last_msg
            args = {"document_name": original_query, "question": last_msg}

        logger.info(f"Tool call: {tool_name}(args={args})")

        # Emit tool_start event so frontend can show activity indicator
        yield ("tool_start", json.dumps({"tool": tool_name, "args": args}))

        # Dispatch to the correct tool executor
        if tool_name == "search_documents":
            search_query = args.pop("query", "")
            # SEARCH-01: extract optional narrowing args BEFORE metadata_filter
            # assembly so they don't leak into the metadata_filter dict (which
            # would be sent to the RPC as JSON garbage).
            folder_path_arg = args.pop("folder_path", None)
            if folder_path_arg:
                try:
                    from app.services.folder_service import normalize_path as _np
                    # Pitfall 4 chokepoint: the LLM is an untrusted source for
                    # paths; canonicalize before forwarding to the RPC.
                    folder_path_arg = _np(folder_path_arg)
                except ValueError:
                    # Non-canonical path (e.g., '..' segment) — fall back to no
                    # narrowing rather than fail the whole search; the LLM may
                    # have hallucinated the path shape.
                    folder_path_arg = None
            scope_arg = args.pop("scope", None)
            if scope_arg not in ("user", "global", "both", None):
                scope_arg = None
            # 'both' is semantically equivalent to no narrowing on scope; the
            # RPC interprets None as "no narrowing" (d.scope = 'both' is never
            # true since the documents.scope CHECK only allows 'user'|'global').
            rpc_scope = None if scope_arg in ("both", None) else scope_arg
            metadata_filter = {k: v for k, v in args.items() if v is not None} or None
            chunks = _execute_search_documents(
                search_query=search_query,
                metadata_filter=metadata_filter,
                user_id=user_id,
                supabase_client=supabase_client,
                folder_path=folder_path_arg,
                scope=rpc_scope,
            )
            # The model's metadata filters use @> containment against document
            # metadata — when it invents keywords/entities that no document
            # carries, the filter silently zeroes the search. Filters narrow;
            # an EMPTY filtered result means the filter was wrong, not the
            # corpus — retry unfiltered.
            if not chunks and metadata_filter:
                logger.info(f"Filtered search empty (filter={metadata_filter}), retrying unfiltered")
                chunks = _execute_search_documents(
                    search_query=search_query,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                    folder_path=folder_path_arg,
                    scope=rpc_scope,
                )
            # The router's paraphrase can drop the very terms that rank the
            # right chunk (same failure mode the SQL path guards against) —
            # also search the user's own wording and merge, paraphrase hits
            # first, capped to the same overall budget. Deliberately unfiltered:
            # this pass is the safety net, it must not inherit a poisoned filter.
            last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            # Run the safety-net pass when the wording differs OR a metadata
            # filter was applied — a verbatim query with a nonzero-but-wrong
            # filtered result otherwise gets no unfiltered look at the corpus.
            if last_user_msg and (metadata_filter or
                                  last_user_msg.strip().lower() != search_query.strip().lower()):
                extra = _execute_search_documents(
                    search_query=last_user_msg,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                    folder_path=folder_path_arg,
                    scope=rpc_scope,
                )
                # Interleave the two rankings (paraphrase first) so the top
                # hits of EACH query survive the cap — appending would let the
                # paraphrase's tail crowd out the user-wording's best hits.
                merged, seen = [], set()
                for pair in zip_longest(chunks, extra):
                    for c in pair:
                        if c is not None and (c["document_id"], c["content"]) not in seen:
                            seen.add((c["document_id"], c["content"]))
                            merged.append(c)
                chunks = merged[:14]
            if chunks:
                from collections import Counter
                doc_counts = Counter(c["file_name"] for c in chunks)
                result_text = "\n\n---\n\n".join(
                    f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
                )
                dominant_doc, dominant_count = doc_counts.most_common(1)[0]
                if dominant_count / len(chunks) >= 0.6:
                    result_text += (
                        f"\n\nNote: {dominant_count}/{len(chunks)} results came from '{dominant_doc}'. "
                        f"If these excerpts are insufficient, suggest the user ask to analyze that document in full."
                    )
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": f"Found {len(chunks)} relevant excerpts"}))
            elif web_search_enabled:
                # Fallback: document search returned nothing, try web search
                logger.info(f"search_documents returned empty, falling back to web_search for: {search_query}")
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": "No documents found"}))
                yield ("tool_start", json.dumps({"tool": "web_search", "args": {"query": search_query}}))
                from app.services.web_search import execute_web_search
                result_text = execute_web_search(search_query)
                tool_name = "web_search"
                yield ("tool_done", json.dumps({"tool": "web_search", "detail": "Web results retrieved"}))
            else:
                result_text = "No relevant documents found."
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": "No documents found"}))

        elif tool_name == "query_structured_data":
            from app.services.sql_tool import execute_sql_query
            question = args.get("question", "")
            # The router's paraphrase can drop parts of the user's intent
            # (e.g. "total load" reduced to "which panels") — always give the
            # SQL generator the original wording too
            last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            if last_user_msg and last_user_msg.strip().lower() not in question.strip().lower():
                question = f"{last_user_msg}\n(Additional context from the assistant: {question})"
            result_text = execute_sql_query(question, user_id, supabase_client)

            # Empty SQL results on a question that MIGHT be a document question
            # (e.g. "how many CCTV cameras are installed?" — cameras are
            # equipment, not a load-schedule row) get a second chance against
            # the document index. The SQL outcome is kept in the context so the
            # model can still answer "not found" for genuinely absent entities
            # instead of hallucinating from unrelated excerpts.
            if _sql_result_is_empty(result_text) and has_documents:
                # Search with the user's own wording — `question` may carry the
                # SQL-augmentation prefix, which dilutes keyword ranking.
                doc_query = last_user_msg or question
                logger.info(f"SQL returned no data, augmenting with search_documents for: {doc_query}")
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": "No rows — checking documents"}))
                yield ("tool_start", json.dumps({"tool": "search_documents", "args": {"query": doc_query}}))
                chunks = _execute_search_documents(
                    search_query=doc_query,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                )
                if chunks:
                    doc_context = "\n\n---\n\n".join(
                        f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
                    )
                    result_text = (
                        "The structured tables returned no rows for this question. "
                        "Document excerpts that may answer it instead:\n\n" + doc_context +
                        "\n\n(If the excerpts do not contain the answer either, say the "
                        "information was not found — do not guess.)"
                    )
                    tool_name = "search_documents"
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": f"Found {len(chunks) if chunks else 0} results"}))

            # If SQL failed and user has documents, fall back to document search
            elif result_text.startswith("SQL query failed") and has_documents:
                logger.info(f"SQL tool failed, falling back to search_documents for: {question}")
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": "SQL failed, falling back"}))
                yield ("tool_start", json.dumps({"tool": "search_documents", "args": {"query": question}}))
                chunks = _execute_search_documents(
                    search_query=question,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                )
                if chunks:
                    result_text = "\n\n---\n\n".join(
                        f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
                    )
                    tool_name = "search_documents"
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": f"Found {len(chunks) if chunks else 0} results"}))
            else:
                yield ("tool_done", json.dumps({"tool": tool_name, "detail": "Query executed"}))

        elif tool_name == "web_search":
            from app.services.web_search import execute_web_search
            query = args.get("query", "")
            result_text = execute_web_search(query)
            yield ("tool_done", json.dumps({"tool": "web_search", "detail": "Web results retrieved"}))

        elif tool_name == "analyze_document":
            from app.services.sub_agent import run_sub_agent
            doc_name = args.get("document_name", "")
            question = args.get("question", "")

            # Resolve document_name → document_id via fuzzy match
            doc = _resolve_document(supabase_client, user_id, doc_name)

            if not doc.data:
                if has_structured_data:
                    # No document by that name, but the user has tabular data —
                    # the "document name" is usually a misrouted data question
                    # (e.g. "explain the total load for block B"), so answer it
                    # with SQL instead of dead-ending.
                    logger.info(f"analyze_document found no match for '{doc_name}', falling back to SQL")
                    yield ("tool_done", json.dumps({"tool": tool_name, "detail": "No matching document, trying tables"}))
                    tool_name = "query_structured_data"
                    yield ("tool_start", json.dumps({"tool": tool_name, "args": {"question": question}}))
                    from app.services.sql_tool import execute_sql_query
                    result_text = execute_sql_query(question or doc_name, user_id, supabase_client)
                    # A doc-flavored question ("how often do the zip tap
                    # filters need changing?") lands here too when its topic
                    # phrase fuzzy-matches no file name — and the tables may
                    # return rows that are RELATED but can't answer it (circuit
                    # locations vs a maintenance schedule). Always add document
                    # excerpts; the answer model picks the source that answers.
                    logger.info("analyze->no doc-name match: combining SQL result with document search")
                    yield ("tool_done", json.dumps({"tool": tool_name, "detail": "Also checking documents"}))
                    tool_name = "search_documents"
                    yield ("tool_start", json.dumps({"tool": tool_name, "args": {"query": question or doc_name}}))
                    chunks = _execute_search_documents(
                        search_query=question or doc_name,
                        metadata_filter=None,
                        user_id=user_id,
                        supabase_client=supabase_client,
                    )
                    if chunks:
                        doc_context = "\n\n---\n\n".join(
                            f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
                        )
                        sql_part = "" if _sql_result_is_empty(result_text) else (
                            "Tabular data result:\n" + result_text + "\n\n---\n\n"
                        )
                        result_text = sql_part + "Document excerpts:\n\n" + doc_context
                else:
                    result_text = f"No document matching '{doc_name}' found."
            else:
                doc_id = doc.data[0]["id"]
                actual_name = doc.data[0]["file_name"]
                sub_agent_result = ""
                for evt_type, evt_data in run_sub_agent(doc_id, actual_name, question, user_id, supabase_client):
                    yield (evt_type, evt_data)
                    if evt_type == "sub_agent_done":
                        sub_agent_result = evt_data
                result_text = sub_agent_result

        elif tool_name == "list_files":
            from app.services.exploration_tools.list_files import list_files as _list_files
            from app.services.exploration_tools.schemas import ListFilesArgs
            try:
                parsed_args = ListFilesArgs(**args)
            except Exception as e:
                result_text = json.dumps({
                    "tool": "list_files",
                    "error": "INVALID_ARGS",
                    "message": str(e),
                })
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": "Invalid arguments",
                }))
            else:
                tool_result = _list_files(parsed_args, user_id, supabase_client)
                result_text = json.dumps(tool_result)
                total = tool_result.get("total", 0) if isinstance(tool_result, dict) else 0
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": f"{total} entries at {parsed_args.path}",
                }))

        elif tool_name == "tree":
            from app.services.exploration_tools.tree import tree as _tree
            from app.services.exploration_tools.schemas import TreeArgs
            try:
                parsed_args = TreeArgs(**args)
            except Exception as e:
                result_text = json.dumps({
                    "tool": "tree",
                    "error": "INVALID_ARGS",
                    "message": str(e),
                })
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": "Invalid arguments",
                }))
            else:
                tool_result = _tree(parsed_args, user_id, supabase_client)
                result_text = json.dumps(tool_result)
                if isinstance(tool_result, dict):
                    tf = tool_result.get("total_folders", 0)
                    td = tool_result.get("total_docs", 0)
                    detail = f"{tf} folders, {td} docs"
                else:
                    detail = "tree complete"
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": detail,
                }))

        elif tool_name == "glob":
            from app.services.exploration_tools.glob_match import glob_match as _glob
            from app.services.exploration_tools.schemas import GlobArgs
            try:
                parsed_args = GlobArgs(**args)
            except Exception as e:
                result_text = json.dumps({
                    "tool": "glob",
                    "error": "INVALID_ARGS",
                    "message": str(e),
                })
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": "Invalid arguments",
                }))
            else:
                tool_result = _glob(parsed_args, user_id, supabase_client)
                result_text = json.dumps(tool_result)
                if isinstance(tool_result, dict):
                    tm = tool_result.get("total_matches", 0)
                    detail = f"{tm} matches for {parsed_args.pattern!r}"
                else:
                    detail = "glob complete"
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": detail,
                }))

        elif tool_name == "read_document":
            from app.services.exploration_tools.read_document import read_document as _read_document
            from app.services.exploration_tools.schemas import ReadDocumentArgs
            try:
                parsed_args = ReadDocumentArgs(**args)
            except Exception as e:
                result_text = json.dumps({
                    "tool": "read_document",
                    "error": "INVALID_ARGS",
                    "message": str(e),
                })
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": "Invalid arguments",
                }))
            else:
                tool_result = _read_document(parsed_args, user_id, supabase_client)
                result_text = json.dumps(tool_result)
                if isinstance(tool_result, dict):
                    if tool_result.get("error"):
                        detail = f"error: {tool_result['error']}"
                    elif tool_result.get("status") == "pending_reindex":
                        detail = "pending_reindex"
                    else:
                        sl = tool_result.get("start_line", 0)
                        el = tool_result.get("end_line", 0)
                        tl = tool_result.get("total_lines", 0)
                        detail = f"lines {sl}-{el}/{tl}"
                else:
                    detail = "read_document complete"
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": detail,
                }))

        elif tool_name == "grep":
            from app.services.exploration_tools.grep import grep as _grep
            from app.services.exploration_tools.schemas import GrepArgs
            try:
                parsed_args = GrepArgs(**args)
            except Exception as e:
                result_text = json.dumps({
                    "tool": "grep",
                    "error": "INVALID_ARGS",
                    "message": str(e),
                })
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": "Invalid arguments",
                }))
            else:
                tool_result = _grep(parsed_args, user_id, supabase_client)
                result_text = json.dumps(tool_result)
                if isinstance(tool_result, dict):
                    if tool_result.get("error"):
                        detail = f"error: {tool_result['error']}"
                    else:
                        th = tool_result.get("total_hits", 0)
                        detail = f"{th} hits for {parsed_args.pattern!r}"
                else:
                    detail = "grep complete"
                yield ("tool_done", json.dumps({
                    "tool": tool_name,
                    "detail": detail,
                }))

        elif tool_name == "explore_knowledge_base":
            # Phase 5: forward generator events from run_explorer_sub_agent and
            # capture the compact summary as result_text. Lazy import avoids the
            # openai_client.py <-> sub_agent.py circular cycle (Pitfall 1).
            from app.services.sub_agent import run_explorer_sub_agent

            query_arg = args.get("query", "")
            if not query_arg or not query_arg.strip():
                result_text = "explore_knowledge_base called with empty query."
            else:
                sub_agent_result = ""
                for evt_type, evt_data in run_explorer_sub_agent(
                    query_arg, user_id, supabase_client,
                ):
                    yield (evt_type, evt_data)
                    if evt_type == "sub_agent_done":
                        sub_agent_result = evt_data
                result_text = sub_agent_result or (
                    "Exploration completed without a summary."
                )
                # Single-fact questions misrouted here often end in an empty
                # exploration summary — always append a direct search pass so
                # the answer model sees the actual excerpts too.
                last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
                extra = _execute_search_documents(
                    search_query=last_user_msg or query_arg,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                )
                if extra:
                    result_text += "\n\n---\n\nDirect search excerpts:\n\n" + "\n\n---\n\n".join(
                        f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in extra
                    )

        else:
            logger.warning(f"Unknown tool: {tool_name}")
            result_text = f"Unknown tool: {tool_name}"

        # Context injection for the final answer (skip the tool round-trip)
        # Truncate very large results to avoid empty Gemini responses
        truncated_result = result_text[:60000] if len(result_text) > 60000 else result_text
        system_with_context = f"""You are a helpful assistant. Use the provided tool results to answer the user's question accurately.
If the tool encountered an error, explain the issue to the user in simple terms and suggest they rephrase their question.
If the results do not contain enough information, clearly state that the available documents do not contain the answer. Do NOT dump or echo the raw tool results back to the user. Instead, briefly explain what information was found (if any) and suggest the user try a different query or upload a document that might contain the answer. You may answer from general knowledge if applicable, but clearly label it as such.
When citing web sources, include the URLs.
{OUTPUT_FORMAT_RULES}

Tool ({tool_name}) results:
{truncated_result}"""

        # Stream the final answer without tools (avoids thought_signature issue)
        response2 = client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_with_context, temperature=0.2),
        )

        has_response2_text = False
        for chunk in response2:
            if chunk.text:
                has_response2_text = True
                yield ("token", chunk.text)

        # Safeguard: if streaming returned nothing, try non-streaming as fallback
        if not has_response2_text:
            logger.warning(f"Context injection streaming returned empty for tool={tool_name}, trying non-streaming fallback")
            try:
                fallback = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_with_context, temperature=0.2),
                )
                if fallback.candidates and fallback.candidates[0].content and fallback.candidates[0].content.parts:
                    for part in fallback.candidates[0].content.parts:
                        if part.text:
                            has_response2_text = True
                            yield ("token", part.text)
            except Exception as e:
                logger.warning(f"Non-streaming fallback also failed: {e}")

        # Last resort: yield the tool result directly
        if not has_response2_text and result_text:
            logger.warning(f"All context injection attempts failed for tool={tool_name}, yielding raw result")
            yield ("token", result_text)
    elif forced_tool == "analyze_document" and has_documents:
        # Gemini didn't call any tool but we detected summarization intent — force analyze_document
        logger.info("Gemini skipped tools but summarization detected — forcing analyze_document")
        last_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        tool_name = "analyze_document"
        args = {"document_name": last_msg, "question": last_msg}
        yield ("tool_start", json.dumps({"tool": tool_name, "args": args}))

        from app.services.sub_agent import run_sub_agent
        doc_name = args.get("document_name", "")
        question = args.get("question", "")

        doc = _resolve_document(supabase_client, user_id, doc_name)

        if not doc.data:
            if has_structured_data:
                # Same fallback chain as the main analyze_document handler: a
                # data question misdetected as summarization shouldn't dead-end,
                # and a doc question whose topic matches no FILE NAME must still
                # reach the document content when the tables come back empty.
                logger.info(f"Forced analyze_document found no match for '{doc_name}', falling back to SQL + doc search")
                from app.services.sql_tool import execute_sql_query
                result_text = execute_sql_query(question or doc_name, user_id, supabase_client)
                chunks = _execute_search_documents(
                    search_query=question or doc_name,
                    metadata_filter=None,
                    user_id=user_id,
                    supabase_client=supabase_client,
                )
                if chunks:
                    doc_context = "\n\n---\n\n".join(
                        f"[Source: {c['file_name']}]\n{_windowed_excerpt(_current_question.get(), c['content'])}" for c in chunks
                    )
                    sql_part = "" if _sql_result_is_empty(result_text) else (
                        "Tabular data result:\n" + result_text + "\n\n---\n\n"
                    )
                    result_text = sql_part + "Document excerpts:\n\n" + doc_context
            else:
                result_text = f"No document matching '{doc_name}' found."
        else:
            doc_id = doc.data[0]["id"]
            actual_name = doc.data[0]["file_name"]
            sub_agent_result = ""
            for evt_type, evt_data in run_sub_agent(doc_id, actual_name, question, user_id, supabase_client):
                yield (evt_type, evt_data)
                if evt_type == "sub_agent_done":
                    sub_agent_result = evt_data
            result_text = sub_agent_result

        if result_text:
            truncated_result = result_text[:60000] if len(result_text) > 60000 else result_text
            system_with_context = f"""You are a helpful assistant. Use the provided tool results to answer the user's question accurately.
{OUTPUT_FORMAT_RULES}

Tool (analyze_document) results:
{truncated_result}"""
            response2 = client.models.generate_content_stream(
                model=model, contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_with_context, temperature=0.2),
            )
            for chunk in response2:
                if chunk.text:
                    yield ("token", chunk.text)
        yield ("tool_done", json.dumps({"tool": "analyze_document", "detail": "Document analyzed"}))
    else:
        # No tool call — LLM responded directly
        has_text = False
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    has_text = True
                    yield ("token", part.text)
        if not has_text:
            logger.warning(f"Gemini returned empty response (finish_reason={response.candidates[0].finish_reason if response.candidates else 'no candidates'})")
            yield ("token", "I'm sorry, I couldn't generate a response. Please try again.")

    yield ("done", "")

