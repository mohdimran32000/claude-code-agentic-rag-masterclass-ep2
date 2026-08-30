"""Record Gemini token usage on LangSmith runs — without ever breaking a request.

WHY THIS EXISTS
LangSmith only records token counts on runs typed `llm`. Every Gemini call in this
app sits *inside* a function traced as `chain` or `tool` (rerank_chunks,
query_structured_data, extract_metadata), so LangSmith shows how long each step took
but not what it cost. This module adds the missing `llm` run.

THE SAFETY CONTRACT
Observability must never be able to break the thing it observes. So:

  * The Gemini call itself is made in a plain, untraced code path.
  * Tracing is attempted around it; if ANYTHING in the tracing layer raises — client
    not configured, LangSmith unreachable, DNS blip, API change — the exception is
    swallowed and the caller still gets its response.
  * A tracing failure is logged at debug level once per process, not per call, so a
    LangSmith outage cannot flood the logs.

Net effect: worst case you lose visibility, never a request.

USAGE
    from app.services.llm_usage import generate_with_usage
    response = generate_with_usage(client, model=..., contents=..., config=...,
                                   name="rerank")
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_warned = False


def _usage_dict(response: Any) -> dict:
    """Pull token counts off a google-genai response into LangSmith's shape."""
    u = getattr(response, "usage_metadata", None)
    if u is None:
        return {}
    inp = getattr(u, "prompt_token_count", None) or 0
    out = getattr(u, "candidates_token_count", None) or 0
    thoughts = getattr(u, "thoughts_token_count", None) or 0
    total = getattr(u, "total_token_count", None) or (inp + out + thoughts)
    return {
        "input_tokens": int(inp),
        "output_tokens": int(out) + int(thoughts),
        "total_tokens": int(total),
    }


def _record(name: str, model: str, contents: Any, response: Any) -> None:
    """Attach an `llm` child run carrying the token usage. Never raises."""
    global _warned
    try:
        from langsmith.run_helpers import get_current_run_tree

        parent = get_current_run_tree()
        if parent is None:
            return  # not inside a traced call — nothing to attach to
        usage = _usage_dict(response)
        if not usage:
            return
        child = parent.create_child(
            name=name,
            run_type="llm",
            inputs={"model": model, "prompt_chars": len(str(contents))},
        )
        child.end(outputs={"usage_metadata": usage})
        child.post()
    except Exception as e:  # noqa: BLE001 - observability must never break a request
        if not _warned:
            _warned = True
            logger.debug(f"LangSmith usage recording unavailable ({type(e).__name__}: {e}); "
                         "continuing without token traces")


def generate_with_usage(client, *, model: str, contents: Any, config: Any = None,
                        name: str = "gemini_call"):
    """Call Gemini, then best-effort record its token usage on the current trace.

    The call is made FIRST and returned unconditionally. Recording happens after and
    cannot affect the return value or raise.
    """
    response = (client.models.generate_content(model=model, contents=contents, config=config)
                if config is not None
                else client.models.generate_content(model=model, contents=contents))
    _record(name, model, contents, response)
    return response
