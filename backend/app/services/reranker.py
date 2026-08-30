import logging
from typing import List

from google import genai
from google.genai import types
from pydantic import BaseModel
from langsmith import traceable

from app.services.settings import get_llm_api_key, get_llm_model, get_reranking_provider, get_cohere_api_key

logger = logging.getLogger(__name__)


class ChunkRelevance(BaseModel):
    index: int
    score: float


class RerankResult(BaseModel):
    rankings: list[ChunkRelevance]


# Relevance scoring is a cheap classification task — pin a fast non-thinking
# model instead of the (possibly thinking) chat model; measured 21.5s/rerank on
# gemini-3-flash-preview vs a few seconds on flash-lite. The "-latest" alias
# (not a pinned dated model) because Google retires dated lite models for new
# API accounts ("gemini-2.5-flash-lite is no longer available to new users").
RERANK_MODEL = "gemini-flash-lite-latest"


def _rerank_gemini(query: str, chunks: List[str], top_k: int) -> List[str]:
    """Use Gemini to score each chunk's relevance to the query."""
    client = genai.Client(api_key=get_llm_api_key())
    model = RERANK_MODEL

    # Score on a bounded, query-focused snippet — full OCR-table chunks (median
    # ~3.6k chars, max ~21k) make the scoring call slow for no ranking gain,
    # but a plain head-truncation hides evidence that sits deep in a chunk
    # (e.g. a warranty letter at offset ~5k of a 7k chunk). So: head + a
    # window around the first query-term hit beyond the head.
    def _snippet(chunk: str) -> str:
        head = chunk[:600]
        low = chunk.lower()
        # One window per DISTINCT query term's first hit (longest terms first —
        # a rarity proxy). First+last-only windowing left a gap in the middle
        # where the decisive fact often sits (e.g. "12 months" between an early
        # heading mention and a late T&C paragraph).
        terms = sorted({t for t in query.lower().split() if len(t) >= 4},
                       key=len, reverse=True)
        windows, covered = [], []
        for t in terms:
            if len(covered) >= 3:
                break
            pos = low.find(t, 600)
            if pos == -1:
                continue
            start = max(600, pos - 200)
            if any(abs(start - c) < 500 for c in covered):
                continue
            covered.append(start)
            windows.append(chunk[start:start + 500])
        if not windows:
            return chunk[:1200]
        return head + " […] " + " […] ".join(windows)

    chunk_list = "\n\n".join(
        f"[Chunk {i}]: {_snippet(chunk)}"
        for i, chunk in enumerate(chunks)
    )

    prompt = f"""Score each chunk's relevance to the query on a scale of 0.0 (irrelevant) to 1.0 (highly relevant).

Relevance means the chunk ANSWERS the query, not merely shares its topic:
- A chunk that states the exact requested value (a duration, count, model, name, date) for the exact subject asked about scores 0.9-1.0.
- Chunks about the same topic but a DIFFERENT subject (e.g. another vendor's warranty when the query names a specific vendor) score at most 0.4.
- Chunks that only discuss related terms without the requested fact score at most 0.5.

Query: {query}

{chunk_list}

Return a JSON object with a "rankings" array, one entry per chunk. Each element should have "index" (chunk number) and "score" (0.0-1.0). Include every chunk — do not filter."""

    from app.services.llm_usage import generate_with_usage

    response = generate_with_usage(
        client,
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RerankResult,
            temperature=0.0,
        ),
        name="rerank_llm",
    )
    result = RerankResult.model_validate_json(response.text)
    sorted_rankings = sorted(result.rankings, key=lambda r: r.score, reverse=True)[:top_k]
    return [chunks[r.index] for r in sorted_rankings if r.index < len(chunks)]


def _rerank_cohere_scored(query: str, chunks: List[str], top_k: int, attempts: int = 3):
    """Cohere rerank, keeping the relevance scores and retrying transient failures.

    The scores were thrown away before. They are the only calibrated signal the
    system has for "none of this actually answers the question", which is what
    the abstain gate needs.

    Retry matters because the previous behaviour returned the unranked RRF top-k
    on ANY exception, with a log line and no signal to the caller - so a Cohere
    blip and a good rerank were indistinguishable downstream.

    A 4xx response (bad/expired key, malformed request, exhausted quota) is
    the one class of failure retrying cannot fix, so it is raised immediately
    on the first attempt rather than retried — see the `status_code` check
    below.
    """
    import cohere, time
    api_key = get_cohere_api_key()
    if not api_key:
        logger.warning("Cohere API key not set, skipping reranking")
        return [(c, None) for c in chunks[:top_k]]
    # cohere 7.0.8 defaults to a 300s request timeout AND retries internally,
    # so with no override here a single hung/rate-limited call plus this
    # function's own 3 attempts could take ~15 minutes to reach the RRF
    # fallback below — measured against the owner's exhausted-quota key
    # (2026-08-29). A short client-side timeout plus max_retries=0 hands ALL
    # retry policy to the loop below, which (unlike the client) can tell a
    # transient failure apart from a 4xx that will never succeed.
    client = cohere.ClientV2(api_key=api_key, timeout=10.0, max_retries=0)
    last = None
    for attempt in range(attempts):
        try:
            resp = client.rerank(model="rerank-v3.5", query=query,
                                 documents=chunks, top_n=top_k)
            return [(chunks[r.index], float(r.relevance_score)) for r in resp.results]
        except Exception as e:
            last = e
            status = getattr(e, "status_code", None)
            if status is not None and 400 <= status < 500:
                # Not transient — a bad/expired key (401) or a malformed
                # request (400) will fail identically on every retry, and an
                # exhausted quota surfaces as a 4xx too. Fail fast instead of
                # burning the full retry budget on something retrying can
                # never fix.
                logger.warning(f"Cohere rerank failed with {status} (not retrying — "
                                f"not a transient failure): {e}")
                raise
            logger.warning(f"Cohere rerank attempt {attempt + 1}/{attempts} failed: {e}")
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last


@traceable(name="rerank_chunks_scored", run_type="chain")
def rerank_chunks_scored(query: str, chunks: List[str], top_k: int = 5):
    """Reranked (text, score) pairs. score is None when reranking did not run."""
    if len(chunks) <= top_k:
        return [(c, None) for c in chunks]
    provider = get_reranking_provider()
    try:
        if provider == "cohere":
            return _rerank_cohere_scored(query, chunks, top_k)
        return [(c, None) for c in _rerank_gemini(query, chunks, top_k)]
    except Exception as e:
        logger.warning(f"Reranking failed ({provider}) after retries, "
                       f"falling back to RRF order: {e}")
        return [(c, None) for c in chunks[:top_k]]


@traceable(name="rerank_chunks", run_type="chain")
def rerank_chunks(query: str, chunks: List[str], top_k: int = 5) -> List[str]:
    """Backwards-compatible: reranked chunk texts only."""
    return [t for t, _ in rerank_chunks_scored(query, chunks, top_k)]
