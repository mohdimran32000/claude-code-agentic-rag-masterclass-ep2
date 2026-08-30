import json
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from app.auth import get_current_user, get_supabase_client
from app.models.schemas import MessageCreate, MessageResponse
from app.services.openai_client import stream_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/threads/{thread_id}/messages", tags=["messages"])


@router.get("", response_model=list[MessageResponse])
async def get_messages(thread_id: str, user_id: str = Depends(get_current_user)):
    supabase = get_supabase_client()
    thread = supabase.table("threads").select("id").eq("id", thread_id).eq("user_id", user_id).maybe_single().execute()
    if not thread.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    return supabase.table("messages").select("*").eq("thread_id", thread_id).order("created_at", desc=False).execute().data


@router.post("")
async def send_message(
    thread_id: str,
    body: MessageCreate,
    user_id: str = Depends(get_current_user),
):
    supabase = get_supabase_client()

    thread = supabase.table("threads").select("*").eq("id", thread_id).eq("user_id", user_id).maybe_single().execute()
    if not thread.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    supabase.table("messages").insert({
        "thread_id": thread_id,
        "user_id": user_id,
        "role": "user",
        "content": body.content,
    }).execute()

    # Stateless completions: load full history from DB
    # Filter out empty assistant messages (can happen if a tool call's context injection failed)
    history = supabase.table("messages").select("role, content").eq("thread_id", thread_id).order("created_at", desc=False).execute()
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history.data
        if m["content"] and m["content"].strip()
    ]

    # WR-01 (Phase 6 review): include scope='global' rows. The pre-Phase-6
    # filter only counted user-owned rows, so a user whose entire corpus is in
    # Shared (no personal uploads yet) would never see the search tool enabled
    # even though the global docs are queryable. Mirror the or_() pattern used
    # in folder_service.list_folder for the union "user's own OR global".
    has_documents = False
    has_structured_data = False
    try:
        ready_docs = (
            supabase.table("documents")
            .select("id")
            .or_(f"and(scope.eq.user,user_id.eq.{user_id}),and(scope.eq.global,user_id.is.null)")
            .eq("status", "ready")
            .limit(1)
            .execute()
        )
        has_documents = bool(ready_docs.data)
    except Exception as e:
        logger.warning(f"Document check failed (non-fatal): {e}")

    # Check if user has structured data (enables Text-to-SQL tool).
    # WR-01 NOTE: structured_data has no `scope` column yet (migration 009 only
    # has user_id NOT NULL — see REVIEW-FIX.md follow-up). Once Phase 7+
    # extends the two-scope model to structured_data, swap this to the same
    # or_() union pattern as ready_docs above.
    structured_tables = []
    try:
        struct_data = (
            supabase.table("structured_data")
            .select("table_name,columns")
            .eq("user_id", user_id)
            .order("table_name")
            .execute()
        )
        has_structured_data = bool(struct_data.data)
        structured_tables = struct_data.data or []
    except Exception as e:
        logger.warning(f"Structured data check failed (non-fatal): {e}")

    def event_generator():
        full_response = ""
        tool_metadata = None
        try:
            for event_type, data in stream_response(
                messages=messages,
                thread_id=thread_id,
                user_id=user_id,
                supabase_client=supabase,
                has_documents=has_documents,
                has_structured_data=has_structured_data,
                structured_tables=structured_tables,
                manual_metadata_filter=body.metadata_filter,
            ):
                if event_type == "token":
                    full_response += data
                    yield json.dumps({"type": "token", "content": data})
                elif event_type == "tool_thinking":
                    parsed = json.loads(data)
                    yield json.dumps({"type": "tool_thinking", **parsed})
                elif event_type == "tool_start":
                    parsed = json.loads(data)
                    yield json.dumps({"type": "tool_start", **parsed})
                elif event_type == "tool_done":
                    parsed = json.loads(data)
                    yield json.dumps({"type": "tool_done", **parsed})
                elif event_type == "sub_agent_start":
                    # Phase 5: refactored to support BOTH analyze_document (legacy
                    # — no agent_name in payload) AND explore_knowledge_base (new
                    # — payload carries agent_name='explore_knowledge_base'). The
                    # accumulator is a list of slots; each new sub_agent_start
                    # appends a fresh slot rather than overwriting [0].
                    parsed = json.loads(data)
                    sub_agent_id = str(uuid.uuid4())
                    if not tool_metadata:
                        tool_metadata = {"tools_used": []}
                    elif "tools_used" not in tool_metadata:
                        tool_metadata["tools_used"] = []
                    # Legacy `analyze_document` events have no agent_name field —
                    # default to "analyze_document" so older flows still persist
                    # the same `tool: analyze_document` value frontend expects.
                    agent_name = parsed.get("agent_name", "analyze_document")
                    slot = {
                        "tool": agent_name,
                        "sub_agent_id": sub_agent_id,
                        "tool_calls": [],
                    }
                    if agent_name == "analyze_document":
                        slot["document_name"] = parsed.get("document_name", "")
                    elif agent_name == "explore_knowledge_base":
                        slot["question"] = parsed.get("question", "")
                    tool_metadata["tools_used"].append(slot)
                    yield json.dumps({
                        "type": "sub_agent",
                        "agent_name": agent_name,
                        "event": "start",
                        "payload": {"sub_agent_id": sub_agent_id, **parsed},
                    })
                elif event_type == "sub_agent_tool_start":
                    # Phase 5 NEW (Explorer-only — analyze_document never emits this):
                    # Append the in-flight inner tool call to the most recent slot's
                    # tool_calls array. Result_preview is filled in on tool_done.
                    parsed = json.loads(data)
                    if tool_metadata and tool_metadata.get("tools_used"):
                        slot = tool_metadata["tools_used"][-1]
                        slot.setdefault("tool_calls", []).append({
                            "tool": parsed.get("tool", ""),
                            "args": parsed.get("args", {}),
                            "turn": parsed.get("turn"),
                        })
                    yield json.dumps({
                        "type": "sub_agent",
                        "agent_name": "explore_knowledge_base",
                        "event": "tool_start",
                        "payload": parsed,
                    })
                elif event_type == "sub_agent_tool_done":
                    # Phase 5 NEW: update the LAST in-flight tool_call in the most
                    # recent slot with its result_preview (300-char cap — V8 + matches
                    # Phase 4's result_preview discipline at messages.py:100 LOCKED).
                    parsed = json.loads(data)
                    if tool_metadata and tool_metadata.get("tools_used"):
                        slot = tool_metadata["tools_used"][-1]
                        if slot.get("tool_calls"):
                            slot["tool_calls"][-1]["result_preview"] = (
                                parsed.get("result_preview", "")[:300]
                            )
                    yield json.dumps({
                        "type": "sub_agent",
                        "agent_name": "explore_knowledge_base",
                        "event": "tool_done",
                        "payload": parsed,
                    })
                elif event_type == "sub_agent_token":
                    # Token stream from the sub-agent's compact summary.
                    # Generalized envelope MUST carry agent_name (uniform contract
                    # across all 5 sub-agent events — Phase 6 frontend routes by
                    # agent_name to the correct sub-agent slot). Resolve from the
                    # most recent slot in the accumulator (the active sub-agent).
                    agent_name = "analyze_document"  # legacy default
                    if tool_metadata and tool_metadata.get("tools_used"):
                        agent_name = tool_metadata["tools_used"][-1].get("tool", "analyze_document")
                    yield json.dumps({
                        "type": "sub_agent",
                        "agent_name": agent_name,
                        "event": "token",
                        "payload": {"content": data},
                    })
                elif event_type == "sub_agent_done":
                    # Phase 5 refactor: write to tools_used[-1] (LAST slot) instead
                    # of [0] — supports multi-sub-agent-per-message (e.g.
                    # analyze_document + explore_knowledge_base in one assistant
                    # turn). The 300-char cap on sub_agent_result matches Phase 4
                    # discipline (V8 data protection).
                    # Generalized envelope MUST carry agent_name (uniform contract
                    # across all 5 sub-agent events). Resolve from the most recent
                    # slot in the accumulator (the active sub-agent that is closing).
                    agent_name = "analyze_document"  # legacy default
                    if tool_metadata and tool_metadata.get("tools_used"):
                        slot = tool_metadata["tools_used"][-1]
                        slot["sub_agent_result"] = data[:300]
                        agent_name = slot.get("tool", "analyze_document")
                    yield json.dumps({
                        "type": "sub_agent",
                        "agent_name": agent_name,
                        "event": "done",
                        "payload": {"summary": data[:300]},
                    })
                elif event_type == "done":
                    yield json.dumps({"type": "done"})
        except Exception as e:
            logger.error(f"Stream error for thread {thread_id}: {e}", exc_info=True)
            yield json.dumps({"type": "error", "content": str(e)})
            yield json.dumps({"type": "done"})
            return

        # Only persist if we got a non-empty response (avoids corrupting conversation history)
        if full_response.strip():
            insert_data = {
                "thread_id": thread_id,
                "user_id": user_id,
                "role": "assistant",
                "content": full_response,
            }
            if tool_metadata:
                # Pass dict directly — Supabase/PostgREST encodes JSONB automatically.
                # Pre-Phase-6 code wrapped this in json.dumps(), which stored a JSON
                # string scalar in the JSONB column. On read-back the value came back
                # as a string, breaking Optional[dict] response validation and 500'ing
                # GET /api/threads/{id}/messages whenever a Sub-Agent message was loaded.
                insert_data["tool_metadata"] = tool_metadata
            supabase.table("messages").insert(insert_data).execute()
        else:
            logger.warning(f"Empty assistant response for thread {thread_id} — not persisting")
        supabase.table("threads").update({"updated_at": "now()"}).eq("id", thread_id).execute()

    return EventSourceResponse(event_generator())
