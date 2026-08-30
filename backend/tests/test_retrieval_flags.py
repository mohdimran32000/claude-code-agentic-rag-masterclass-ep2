import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import retrieval_flags as rf

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {'' if cond else detail}")
    if not cond: FAILS.append(name)

os.environ.pop("RETRIEVAL_CHUNK_IDENTITY", None)
check("a flag defaults to OFF", rf.flag("chunk_identity") is False)

os.environ["RETRIEVAL_CHUNK_IDENTITY"] = "1"
check("'1' turns a flag on", rf.flag("chunk_identity") is True)
os.environ["RETRIEVAL_CHUNK_IDENTITY"] = "true"
check("'true' turns a flag on", rf.flag("chunk_identity") is True)
os.environ["RETRIEVAL_CHUNK_IDENTITY"] = "0"
check("'0' turns a flag off", rf.flag("chunk_identity") is False)
os.environ["RETRIEVAL_CHUNK_IDENTITY"] = "no"
check("an unrecognised value is OFF, never a crash", rf.flag("chunk_identity") is False)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
