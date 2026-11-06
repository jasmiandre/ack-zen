"""ACK Stage 1: public landing helpers. All visible copy lives in content.json (editable without touching code)."""
import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_PATH = os.path.join(HERE, "content.json")
WIB = timezone(timedelta(hours=7), "WIB")  # Asia/Jakarta has no DST; stdlib-only, no tzdata needed

_cache = {"mtime": None, "data": None}


def load_content(path=CONTENT_PATH):
    """Re-read only when the file changed; a bad edit raises (fail loud at startup, 500 later)."""
    m = os.stat(path).st_mtime_ns
    if path != CONTENT_PATH or _cache["mtime"] != m:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if path != CONTENT_PATH:
            return data
        _cache.update(mtime=m, data=data)
    return _cache["data"]


def countdown(iso, now=None):
    """Exact remaining time to `iso` (Asia/Jakarta). Returns dict d/h/m/s + expired flag; never negative."""
    target = datetime.fromisoformat(iso)
    now = now or datetime.now(WIB)
    left = int((target - now).total_seconds())
    if left <= 0:
        return {"d": 0, "h": 0, "m": 0, "s": 0, "expired": True}
    d, r = divmod(left, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return {"d": d, "h": h, "m": m, "s": s, "expired": False}


PUBLIC_CSP = (
    "default-src 'none'; "
    "script-src 'self' "
    "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js "
    "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js "
    "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/ScrollTrigger.min.js "
    "https://cdn.jsdelivr.net/npm/lenis@1.1.13/dist/lenis.min.js; "
    "style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)
