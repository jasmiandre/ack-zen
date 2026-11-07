"""Stage 4: role-safe dashboard aggregates + member list/pipeline (matchmaker, owner). Audit uses internal IDs only, never PII."""
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import auth
import db

MATCH = {"matchmaker", "owner"}
PIPELINE = ("Baru", "Aktif", "Dalam Perkenalan", "Matched", "Nonaktif")   # separate from members.status (its CHECK is untouched)
PAGE = 20
ENDS_SOON_DAYS = 7


def _now():   # seam for boundary tests
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def stats(conn, roles, now=None):
    """Only the aggregates a role may see; counts only, no PII."""
    now = now or _now()
    day = now.replace(hour=0, minute=0, second=0)
    n = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    out = {}
    if roles & {"matchmaker", "owner"}:
        out["Member baru hari ini (UTC)"] = n("SELECT COUNT(*) FROM members WHERE created_at>=?", _iso(day))
        out["Member baru 7 hari terakhir"] = n("SELECT COUNT(*) FROM members WHERE created_at>=?", _iso(day - timedelta(days=6)))
        out["Respons perkenalan: tertarik"] = n("SELECT COUNT(*) FROM introductions WHERE resp_a='tertarik'") + n("SELECT COUNT(*) FROM introductions WHERE resp_b='tertarik'")
        out["Respons perkenalan: tidak"] = n("SELECT COUNT(*) FROM introductions WHERE resp_a='tidak'") + n("SELECT COUNT(*) FROM introductions WHERE resp_b='tidak'")
        out[f"Program berakhir dalam {ENDS_SOON_DAYS} hari"] = n(
            "SELECT COUNT(*) FROM subscriptions WHERE status='active' AND ends_at>=? AND ends_at<=?",
            _iso(now), _iso(now + timedelta(days=ENDS_SOON_DAYS)))
        out["Follow-up jatuh tempo"] = n("SELECT COUNT(*) FROM members WHERE next_followup_at IS NOT NULL AND next_followup_at<=?",
                                         now.strftime("%Y-%m-%d"))
    if roles & {"finance", "owner"}:
        out["Pembayaran menunggu verifikasi"] = n("SELECT COUNT(*) FROM payments WHERE status='pending' AND proof_file_id IS NOT NULL")
    if roles & {"verifier", "owner"}:
        out["Verifikasi identitas menunggu"] = n("SELECT COUNT(*) FROM members WHERE status='pending'")
    return out


def _cut(today, years):
    try:
        return today.replace(year=today.year - years)
    except ValueError:   # Feb 29 -> non-leap target year: a Feb-29 birth has not turned `years` before Mar 1
        return date(today.year - years, 2, 28)


def _int(raw, lo, hi):
    raw = (raw or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 4 and lo <= int(raw) <= hi else None


def query(conn, args, today=None):
    """-> (rows, total, page, pages, normalised filters). Parameterised; LIKE wildcards escaped."""
    today = today or _now().date()
    w, p, f = [], [], {}
    for k in ("gender", "religion", "ethnicity"):
        v = (args.get(k) or "").strip()[:60]
        if v:
            w.append(f"m.{k}=?"); p.append(v); f[k] = v
    for k, cols in (("domicile", ("m.domicile",)), ("q", ("m.full_name", "m.code"))):
        v = (args.get(k) or "").strip()[:60]
        if v:
            esc = v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            w.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")"); p += [f"%{esc}%"] * len(cols); f[k] = v
    pl = (args.get("pipeline") or "").strip()
    if pl in PIPELINE:
        w.append("COALESCE(m.pipeline,'Baru')=?"); p.append(pl); f["pipeline"] = pl
    pid = _int(args.get("program"), 1, 10**9)
    if pid:
        w.append("(SELECT program_id FROM subscriptions WHERE member_id=m.id ORDER BY id DESC LIMIT 1)=?"); p.append(pid); f["program"] = pid
    amin, amax = _int(args.get("age_min"), 0, 120), _int(args.get("age_max"), 0, 120)
    if amin is not None:
        w.append("m.birth_date<=?"); p.append(_cut(today, amin).isoformat()); f["age_min"] = amin
    if amax is not None:
        w.append("m.birth_date>?"); p.append(_cut(today, amax + 1).isoformat()); f["age_max"] = amax
    if args.get("verified") == "1":
        w.append("m.status='verified'"); f["verified"] = "1"
    where = (" WHERE " + " AND ".join(w)) if w else ""
    total = conn.execute(f"SELECT COUNT(*) FROM members m{where}", p).fetchone()[0]
    pages = max(1, -(-total // PAGE))
    page = min(_int(args.get("page"), 1, 10**6) or 1, pages)
    rows = conn.execute(
        f"SELECT m.id,m.code,m.full_name,m.gender,m.birth_date,m.religion,m.ethnicity,m.domicile,m.status,"
        f"COALESCE(m.pipeline,'Baru') AS pipeline,m.next_followup_at,m.internal_notes,"
        f"(SELECT pr.name FROM subscriptions s JOIN programs pr ON pr.id=s.program_id WHERE s.member_id=m.id ORDER BY s.id DESC LIMIT 1) AS program "
        f"FROM members m{where} ORDER BY m.id DESC LIMIT ? OFFSET ?", [*p, PAGE, (page - 1) * PAGE]).fetchall()
    rows = [{**dict(r), "age": db.age_on(date.fromisoformat(r["birth_date"]), today)} for r in rows]
    return rows, total, page, pages, f


def mount(app, conn, jenv, secret_key, cookie_secure):
    def gate(request):
        tok = request.cookies.get(auth.COOKIE)
        s = auth.load_session(conn, secret_key, tok) if tok else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        if not (s["roles"] & MATCH):
            return None, PlainTextResponse("Forbidden", status_code=403)
        return s, None

    def page(s, status=200, error=None, args=None):
        rows, total, pg, pages, f = query(conn, args or {})
        opts = {k: [r[0] for r in conn.execute(f"SELECT DISTINCT {k} FROM members WHERE {k} IS NOT NULL AND {k}<>'' ORDER BY 1")]
                for k in ("gender", "religion", "ethnicity")}
        qs = urlencode({k: v for k, v in f.items()})
        return HTMLResponse(jenv.get_template("members.html").render(
            sess=s, csrf=s["csrf"], http_warning=not cookie_secure, labels=db.ROLE_LABEL, rows=rows, total=total, page=pg,
            pages=pages, f=f, opts=opts, pipeline=PIPELINE, qs=qs, error=error,
            programs=conn.execute("SELECT id,name FROM programs ORDER BY id").fetchall()), status_code=status)

    @app.get("/anggota")
    async def members(request: Request):
        s, bad = gate(request)
        return bad or page(s, args=request.query_params)

    @app.post("/anggota/{mid}/pipeline")
    async def set_pipeline(request: Request, mid: int):
        s, bad = gate(request)
        if bad:
            return bad
        form = await request.form()
        try:
            if not auth.csrf_ok(s, form.get("csrf_token")):
                return PlainTextResponse("Forbidden", status_code=403)
            pl = str(form.get("pipeline") or "")
            fu = str(form.get("next_followup_at") or "").strip()
            note = str(form.get("internal_notes") or "").strip()
            try:
                if fu:
                    fu = db.parse_birth_date(fu).isoformat()   # strict YYYY-MM-DD parse
            except ValueError:
                fu = None
            if pl not in PIPELINE or fu is None or len(note) > 1000:
                return page(s, 400, "Pipeline/tanggal follow-up (YYYY-MM-DD)/catatan (maks 1000) tidak valid.")
            with db.tx(conn):
                if not conn.execute("UPDATE members SET pipeline=?,next_followup_at=?,internal_notes=? WHERE id=?",
                                    (pl, fu or None, note or None, mid)).rowcount:
                    return PlainTextResponse("Not found", status_code=404)
                db.audit(conn, s["staff_id"], "pipeline_set", f"member={mid} to={pl}")   # notes/names stay out of audit
            return RedirectResponse("/anggota", 303)
        finally:
            await form.close()
