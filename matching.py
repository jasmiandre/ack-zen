"""Stage 4: deterministic matching (top 10, explained, never auto-proposes) + introduction/quota/dinner staff workflow.

Quota policy (programs.intro_quota): NULL = unconfigured -> blocks NEW proposals; 0 = none; expired/non-active subscription
blocks NEW proposals only (existing intros stay visible). A proposal *reserves* capacity (available = quota - consumed - pending
proposed); quota is *consumed* once, when both sides are 'tertarik' (contact opens). Decline / no answer = nothing consumed, no refund.
Pre-v6 (Stage 3) introductions have quota_tracked=0 and are never charged."""
from datetime import date, datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import auth
import db
import register

MATCH = {"matchmaker", "owner"}
BLOCKED_PIPELINE = ("Matched", "Nonaktif")
W_AGE, W_REL, W_DOM, W_ETH, W_EDU = 30, 30, 20, 10, 10
TOP = 10
OUTCOMES = {"berhasil": "Berhasil", "tidak_lanjut": "Tidak lanjut"}


def norm(s):
    return " ".join((s or "").split()).casefold()


def same_marga(a, b):
    x, y = norm(a["marga"]), norm(b["marga"])
    return bool(x and y and x == y)


def _fit(pref_min, pref_max, age):
    if pref_min is None and pref_max is None:
        return None
    lo, hi = pref_min if pref_min is not None else 0, pref_max if pref_max is not None else 200
    d = max(lo - age, age - hi, 0)
    return max(0.0, 1 - d / 10)


def _side(p, o, today):
    """How well candidate `o` fits preferences of `p`. -> {component: (weight, fit 0..1 or None=unknown)}"""
    age = db.age_on(date.fromisoformat(o["birth_date"]), today)
    rel = None
    if norm(p["pref_religion"]):
        rel = 1.0 if norm(p["pref_religion"]) in ("bebas", norm(o["religion"])) else 0.0
    dom = None
    if norm(p["pref_domicile"]):
        a, b = norm(p["pref_domicile"]), norm(o["domicile"])
        dom = 1.0 if b and (a in b or b in a) else 0.0
    return {"usia": (W_AGE, _fit(p["pref_age_min"], p["pref_age_max"], age)), "agama": (W_REL, rel), "domisili": (W_DOM, dom)}


def _common(a, b):
    out = {}
    ea, eb = norm(a["ethnicity"]), norm(b["ethnicity"])
    out["suku"] = (W_ETH, (1.0 if ea == eb else 0.0) if ea and eb else None)
    ra = register.EDUCATION.index(a["education"]) if a["education"] in register.EDUCATION[:-1] else None
    rb = register.EDUCATION.index(b["education"]) if b["education"] in register.EDUCATION[:-1] else None
    out["pendidikan"] = (W_EDU, max(0.0, 1 - abs(ra - rb) / 2) if ra is not None and rb is not None else None)
    return out


def score_pair(a, b, today):
    """Pure. -> (score 0..100, reasons[list[str]], summary). Unknown preference = no points possible, no penalty, no constraint."""
    sides = []
    for tag, p, o in (("A", a, b), ("B", b, a)):
        comp = {**_side(p, o, today), **_common(a, b)}
        poss = sum(w for w, f in comp.values() if f is not None)
        sides.append((tag, comp, (sum(w * f for w, f in comp.values() if f is not None) / poss * 100) if poss else 0.0))
    reasons, good = [], []
    for tag, comp, sc in sides:
        for k, (w, f) in comp.items():
            if f is None:
                reasons.append(f"{k} ({tag}): preferensi belum diisi, tidak dihitung")
            else:
                reasons.append(f"{k} ({tag}): {'sesuai' if f >= 1 else 'sebagian' if f > 0 else 'tidak sesuai'} ({round(w * f)}/{w})")
    for k in ("usia", "agama", "domisili", "suku", "pendidikan"):
        fs = [c[k][1] for _, c, _ in sides if k in c and c[k][1] is not None]
        if fs and min(fs) >= 0.5:
            good.append(k)
    return round(sum(s for _, _, s in sides) / 2), reasons, ("Cocok: " + ", ".join(good)) if good else "Belum ada kecocokan utama"


def hard_ok(a, b):
    """Pure pair-level gate (members must already be individually eligible)."""
    return bool(a["gender"] and b["gender"] and a["gender"] != b["gender"] and not same_marga(a, b))


def top_matches(subject, cands, today, n=TOP):
    """Pure + deterministic: (-score, id) ordering."""
    out = []
    for c in cands:
        if c["id"] != subject["id"] and hard_ok(subject, c):
            sc, rs, sm = score_pair(subject, c, today)
            out.append({"m": c, "score": sc, "reasons": rs, "summary": sm})
    out.sort(key=lambda x: (-x["score"], x["m"]["id"]))
    return out[:n]


# ---------------------------------------------------------------- DB layer
_BASE = ("SELECT m.*, COALESCE(m.pipeline,'Baru') AS pl, pr.name AS prog, pr.intro_quota AS quota, s.starts_at AS s_start, s.ends_at AS s_end "
         "FROM members m LEFT JOIN subscriptions s ON s.id=(SELECT MAX(id) FROM subscriptions WHERE member_id=m.id AND status='active') "
         "LEFT JOIN programs pr ON pr.id=s.program_id ")


def capacity(conn, m, now):
    """-> (available or None, why-not str or None) for NEW proposals."""
    if m["prog"] is None:
        return None, "tidak ada langganan aktif"
    if not m["s_end"] or m["s_end"] <= now:
        return None, "langganan sudah berakhir / belum dimulai"
    if m["quota"] is None:
        return None, "kuota program belum dikonfigurasi"
    used = conn.execute("SELECT COUNT(*) FROM introductions WHERE quota_consumed=1 AND (member_a=? OR member_b=?) AND mutual_at>=?",
                        (m["id"], m["id"], m["s_start"] or "")).fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM introductions WHERE quota_tracked=1 AND status='proposed' AND (member_a=? OR member_b=?)",
                        (m["id"], m["id"])).fetchone()[0]
    av = m["quota"] - used - pend
    return av, (None if av >= 1 else "kuota habis (termasuk usulan yang masih menunggu)")


def eligible(conn, m, now):
    """-> reason it is NOT eligible, or None."""
    if m["status"] != "verified":
        return "belum terverifikasi"
    if m["gender"] not in register.GENDER:
        return "gender belum diisi"
    if m["pl"] in BLOCKED_PIPELINE:
        return f"pipeline {m['pl']}"
    if m["marital_status"] in register.REVIEW_STATUS or m["perlu_review"]:
        return "status perlu review"
    return capacity(conn, m, now)[1]


def _pair_blocked(conn, a, b):
    return conn.execute("SELECT 1 FROM introductions WHERE (member_a=? AND member_b=?) OR (member_a=? AND member_b=?)",
                        (a, b, b, a)).fetchone() is not None   # any prior intro, incl. one-sided decline = permanent


def candidates(conn, subject, today, now):
    taken = {r[0] for r in conn.execute("SELECT member_b FROM introductions WHERE member_a=? UNION SELECT member_a FROM introductions WHERE member_b=?",
                                        (subject["id"], subject["id"]))}
    rows = conn.execute(_BASE + "WHERE m.id<>? AND m.gender IS NOT NULL AND m.gender<>?", (subject["id"], subject["gender"])).fetchall()
    return [r for r in rows if r["id"] not in taken and eligible(conn, r, now) is None]


def apply_response(conn, iid, col, choice, now=None):
    """Shared by member portal and staff. Call inside db.tx. -> new status or None if not allowed."""
    r = conn.execute("SELECT * FROM introductions WHERE id=?", (iid,)).fetchone()
    if not r or col not in ("resp_a", "resp_b") or choice not in ("tertarik", "tidak") or r["status"] != "proposed" or r[col]:
        return None
    conn.execute(f"UPDATE introductions SET {col}=? WHERE id=?", (choice, iid))
    x = conn.execute("SELECT resp_a,resp_b FROM introductions WHERE id=?", (iid,)).fetchone()
    new = "declined" if "tidak" in tuple(x) else "mutual" if x[0] == x[1] == "tertarik" else "proposed"
    conn.execute("UPDATE introductions SET status=? WHERE id=?", (new, iid))
    if new == "mutual":   # contact opens -> consume exactly once; declined/pending never consume, nothing is refunded
        conn.execute("UPDATE introductions SET quota_consumed=1, mutual_at=? WHERE id=? AND quota_tracked=1 AND quota_consumed=0",
                     (now or db.now_iso(), iid))
    return new


def propose(conn, subject_id, cand_id, staff_id, today, now):
    """-> (ok, message). One IMMEDIATE tx: rechecks everything, so concurrent proposals cannot oversubscribe the last quota."""
    with db.tx(conn):
        a = conn.execute(_BASE + "WHERE m.id=?", (subject_id,)).fetchone()
        b = conn.execute(_BASE + "WHERE m.id=?", (cand_id,)).fetchone()
        if not a or not b or a["id"] == b["id"]:
            return False, "Member tidak ditemukan."
        if _pair_blocked(conn, a["id"], b["id"]):
            return False, "Pasangan ini sudah pernah diusulkan (aktif, ditolak, atau selesai)."
        for m in (a, b):
            why = eligible(conn, m, now)
            if why:
                return False, f"{m['code']} tidak memenuhi syarat: {why}."
        if not hard_ok(a, b):
            return False, "Pasangan tidak memenuhi syarat (gender sama atau marga sama)."
        sc, _, summary = score_pair(a, b, today)
        conn.execute("INSERT INTO introductions(member_a,member_b,status,proposed_by,created_at,match_reason,quota_tracked) VALUES(?,?,?,?,?,?,1)",
                     (a["id"], b["id"], "proposed", staff_id, db.now_iso(), summary))
        db.audit(conn, staff_id, "intro_propose", f"a={a['id']} b={b['id']} score={sc}")
    return True, "Usulan dibuat; kuota baru terpakai saat kedua pihak tertarik."


def stage_label(r):
    if r["outcome"]:
        return {"dinner_scheduled": "Dinner dijadwalkan", **OUTCOMES}[r["outcome"]]
    if r["status"] == "declined":
        return "Ditolak"
    if r["status"] == "mutual":
        return "Kontak dibuka"
    ra, rb = r["resp_a"], r["resp_b"]
    return "Tertarik: " + ("A" if ra == "tertarik" else "") + ("B" if rb == "tertarik" else "") if "tertarik" in (ra, rb) else "Diusulkan"


def _valid_slot(s):
    try:
        d = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    n = datetime.now(timezone.utc).replace(tzinfo=None)
    return s if 0 < (d - n).total_seconds() <= 366 * 86400 else None


def mount(app, conn, jenv, secret_key, cookie_secure):
    def gate(request):
        tok = request.cookies.get(auth.COOKIE)
        s = auth.load_session(conn, secret_key, tok) if tok else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        return (s, None) if s["roles"] & MATCH else (None, PlainTextResponse("Forbidden", status_code=403))

    def render(name, s, status=200, **ctx):
        return HTMLResponse(jenv.get_template(name).render(sess=s, csrf=s["csrf"], http_warning=not cookie_secure, labels=db.ROLE_LABEL, **ctx),
                            status_code=status)

    def now():
        return db.now_iso()

    def cand_page(s, mid, msg=None, status=200):
        subj = conn.execute(_BASE + "WHERE m.id=?", (mid,)).fetchone()
        if not subj:
            return PlainTextResponse("Not found", status_code=404)
        why = eligible(conn, subj, now())
        av = capacity(conn, subj, now())[0]
        top = [] if why else top_matches(subj, candidates(conn, subj, datetime.now(timezone.utc).date(), now()), datetime.now(timezone.utc).date())
        return render("match.html", s, status, subj=subj, why=why, avail=av, top=top, msg=msg)

    @app.get("/pasangan/{mid}")
    async def view(request: Request, mid: int):
        s, bad = gate(request)
        return bad or cand_page(s, mid)

    @app.post("/pasangan/{mid}/usul")
    async def do_propose(request: Request, mid: int):
        s, bad = gate(request)
        if bad:
            return bad
        form = await request.form()
        try:
            if not auth.csrf_ok(s, form.get("csrf_token")):
                return PlainTextResponse("Forbidden", status_code=403)
            c = str(form.get("candidate") or "")
            if not (c.isascii() and c.isdigit() and len(c) < 10):
                return cand_page(s, mid, "Kandidat tidak valid.", 400)
            ok, msg = propose(conn, mid, int(c), s["staff_id"], datetime.now(timezone.utc).date(), now())
            return cand_page(s, mid, msg, 200 if ok else 400)
        finally:
            await form.close()

    @app.get("/perkenalan")
    async def intros(request: Request):
        s, bad = gate(request)
        return bad or intro_page(s)

    def intro_page(s, msg=None, status=200):
        rows = conn.execute(
            "SELECT i.*, a.code AS ca, b.code AS cb FROM introductions i JOIN members a ON a.id=i.member_a JOIN members b ON b.id=i.member_b "
            "ORDER BY i.id DESC LIMIT 200").fetchall()
        view = [{"r": r, "label": stage_label(r),
                 "slots": conn.execute("SELECT * FROM dinner_slots WHERE introduction_id=? ORDER BY starts_at", (r["id"],)).fetchall()} for r in rows]
        return render("match_intros.html", s, status, rows=view, msg=msg, outcomes=OUTCOMES)

    @app.post("/perkenalan/{iid}/aksi")
    async def act(request: Request, iid: int):
        s, bad = gate(request)
        if bad:
            return bad
        form = await request.form()
        try:
            if not auth.csrf_ok(s, form.get("csrf_token")):
                return PlainTextResponse("Forbidden", status_code=403)
            a, ok = str(form.get("action") or ""), False
            with db.tx(conn):
                r = conn.execute("SELECT * FROM introductions WHERE id=?", (iid,)).fetchone()
                if not r:
                    return PlainTextResponse("Not found", status_code=404)
                if a in ("resp_a", "resp_b"):
                    ok = apply_response(conn, iid, a, str(form.get("choice") or ""), now()) is not None
                elif a == "jadwal" and r["status"] == "mutual" and not r["outcome"] and conn.execute(
                        "SELECT 1 FROM dinner_slots WHERE introduction_id=? AND picked_by IS NOT NULL", (iid,)).fetchone():
                    ok = conn.execute("UPDATE introductions SET outcome='dinner_scheduled' WHERE id=? AND outcome IS NULL", (iid,)).rowcount == 1
                elif a == "hasil" and r["outcome"] == "dinner_scheduled" and form.get("result") in OUTCOMES:
                    ok = conn.execute("UPDATE introductions SET outcome=? WHERE id=? AND outcome='dinner_scheduled'", (form.get("result"), iid)).rowcount == 1
                elif a == "slot" and r["status"] == "mutual" and not r["outcome"]:
                    start, venue = _valid_slot(str(form.get("starts_at") or "").strip()), str(form.get("venue") or "").strip()
                    plat = conn.execute("SELECT 1 FROM subscriptions x JOIN programs p ON p.id=x.program_id WHERE x.member_id IN (?,?) "
                                        "AND x.status='active' AND lower(p.name) LIKE '%platinum%'", (r["member_a"], r["member_b"])).fetchone()
                    if start and 0 < len(venue) <= 120 and plat:
                        conn.execute("INSERT INTO dinner_slots(introduction_id,starts_at,venue) VALUES(?,?,?)", (iid, start, venue))
                        ok = True
                if ok:
                    db.audit(conn, s["staff_id"], "intro_" + a, f"intro={iid}")
            return intro_page(s, "Tersimpan." if ok else "Aksi tidak valid untuk status saat ini.", 200 if ok else 400)
        finally:
            await form.close()
