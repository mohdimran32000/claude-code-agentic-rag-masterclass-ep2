import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond: FAILS.append(name)

# Fake the cohere module so the test never touches the network.
calls = {"n": 0}
init_kwargs = {}
class _R:
    def __init__(self, i, s): self.index, self.relevance_score = i, s
class _Resp:
    def __init__(self, rs): self.results = rs
class _ApiErrorLike(Exception):
    """Stands in for cohere.errors.UnauthorizedError / BadRequestError, etc —
    real ApiError subclasses each carry a genuine .status_code, which is what
    reranker.py's no-retry-on-4xx check (I7) reads."""
    def __init__(self, status_code, msg="bad"):
        super().__init__(msg)
        self.status_code = status_code
class _Client:
    def __init__(self, api_key=None, **kwargs):
        init_kwargs.clear(); init_kwargs.update(kwargs)
    def rerank(self, model, query, documents, top_n):
        calls["n"] += 1
        if calls["n"] < 2:                      # fail once, to exercise the retry
            raise RuntimeError("transient 503")
        return _Resp([_R(1, 0.91), _R(0, 0.42)])
class _Client4xx:
    """A key that will never work — every call fails the same way."""
    def __init__(self, api_key=None, **kwargs):
        init_kwargs.clear(); init_kwargs.update(kwargs)
    def rerank(self, model, query, documents, top_n):
        calls["n"] += 1
        raise _ApiErrorLike(401, "invalid api token")
fake = types.ModuleType("cohere"); fake.ClientV2 = _Client
sys.modules["cohere"] = fake

from app.services import reranker as rr
rr.get_cohere_api_key = lambda: "k"
rr.get_reranking_provider = lambda: "cohere"

print("rerank_chunks_scored()")
calls["n"] = 0
out = rr.rerank_chunks_scored("q", ["alpha", "beta", "gamma"], top_k=2)
check("returns (text, score) pairs", all(isinstance(t, str) and isinstance(s, float)
                                         for t, s in out), str(out))
check("scores are KEPT, not discarded", [s for _, s in out] == [0.91, 0.42], str(out))
check("order follows the reranker, not the input", [t for t, _ in out] == ["beta", "alpha"],
      str(out))
check("a transient failure is RETRIED, not silently swallowed", calls["n"] == 2,
      f"cohere called {calls['n']}x")

print("rerank_chunks() still returns bare strings")
calls["n"] = 1   # no failure this time
out2 = rr.rerank_chunks("q", ["alpha", "beta", "gamma"], top_k=2)
check("existing callers keep a list[str]", out2 == ["beta", "alpha"], str(out2))

print("client is constructed with a short timeout and no internal retries (I7)")
check("a bounded request timeout is set (so 3 attempts stay well under a minute, "
      "not cohere's 300s default x an internal retry)",
      isinstance(init_kwargs.get("timeout"), (int, float)) and 0 < init_kwargs["timeout"] <= 15,
      str(init_kwargs))
check("the client's own internal retries are disabled — our loop owns retry policy",
      init_kwargs.get("max_retries") == 0, str(init_kwargs))

print("a 4xx (bad/expired key, exhausted quota) fails fast without retrying (I7)")
fake.ClientV2 = _Client4xx
calls["n"] = 0
out3 = rr.rerank_chunks_scored("q", ["alpha", "beta", "gamma"], top_k=2)
check("falls back to the un-reranked top_k (RRF order), not left broken",
      out3 == [("alpha", None), ("beta", None)], str(out3))
check("attempted EXACTLY ONCE — a 4xx must never be retried "
      "(previously ~3 attempts x cohere's own internal retries could take ~15 minutes)",
      calls["n"] == 1, f"cohere called {calls['n']}x")

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
