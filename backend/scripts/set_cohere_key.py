"""Copy COHERE_API_KEY from .env into global_settings, and switch reranking to Cohere.

The app reads the key from the DATABASE (`global_settings.cohere_api_key`), not from the
environment — see settings.get_cohere_api_key(). This script bridges the two so the key
only ever lives in .env and the database, never in a chat log or a commit.

Usage:
    cd backend
    venv/Scripts/python scripts/set_cohere_key.py            # write key + switch provider
    venv/Scripts/python scripts/set_cohere_key.py --check    # show current state only
    venv/Scripts/python scripts/set_cohere_key.py --revert   # switch back to gemini

Verifies the key against Cohere's API before writing it, so a typo fails here rather
than silently degrading every future search (rerank_chunks swallows errors and returns
the un-reranked top-k).
"""
import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from supabase import create_client  # noqa: E402


def show(sb) -> dict:
    row = (sb.table("global_settings")
           .select("id,reranking_enabled,reranking_provider,cohere_api_key")
           .execute().data or [{}])[0]
    print(f"  reranking_enabled  : {row.get('reranking_enabled')}")
    print(f"  reranking_provider : {row.get('reranking_provider')}")
    print(f"  cohere_api_key     : {'set' if row.get('cohere_api_key') else 'NOT set'}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    print("BEFORE")
    row = show(sb)
    if not row:
        print("\nERROR: no global_settings row found")
        return 1

    if args.check:
        return 0

    if args.revert:
        sb.table("global_settings").update({"reranking_provider": "gemini"}) \
          .eq("id", row["id"]).execute()
        print("\nreverted to gemini\n\nAFTER")
        show(sb)
        return 0

    key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not key:
        print("\nERROR: COHERE_API_KEY is empty in backend/.env")
        print("       Paste your key after the '=' on that line, then run this again.")
        return 1

    # Verify before writing — a bad key otherwise fails silently at query time.
    print("\nverifying the key against Cohere...")
    try:
        import cohere
        client = cohere.ClientV2(api_key=key)
        r = client.rerank(model="rerank-v3.5", query="fire extinguisher",
                          documents=["a dry powder fire extinguisher, 4 kg",
                                     "a zip hydrotap boiling water unit"], top_n=1)
        top = r.results[0]
        print(f"  OK - Cohere replied, top match index {top.index} "
              f"score {getattr(top, 'relevance_score', 'n/a')}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {type(e).__name__}: {str(e)[:300]}")
        print("  Nothing written. Check the key, or the model name if your plan differs.")
        return 1

    sb.table("global_settings").update({
        "cohere_api_key": key,
        "reranking_provider": "cohere",
        "reranking_enabled": True,
    }).eq("id", row["id"]).execute()

    print("\nAFTER")
    show(sb)
    print("\nNote: settings are cached for 60s in-process, so a running server picks this")
    print("      up within a minute (or on restart).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
