"""Stage 5 final chunk: staff pairing rounds (suggest/edit/save) + post-event mutual picks -> event_connections.
Event connections are SEPARATE from `introductions`: no program quota, no subscription, never touches the program quota path."""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import auth
import db
import pairing

WIB = timezone(timedelta(hours=7))   # ponytail: events.ends_at is naive local time; assumed WIB. Add a tz column if events leave Indonesia.
LEVEL_KEY = "level"                  # event extra_fields answer key holding Pemula/Menengah/Mahir; absent/other => unknown (never invented)
PRESENT = "r.status='checked_in'"


def event_ended(e, now=None):
    try:
        end = datetime.strptime(e["ends_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=WIB)
    except (TypeError, ValueError):
        return False   # no ends_at => never ended; no invented cutoff
    return (now or datetime.now(WIB)) >= end


def participants(conn, eid):
    """checked_in only: [(member_id, code, gender, level)]."""
    out = []
    for r in conn.execute(f"SELECT m.id,m.code,m.gender,r.answers FROM event_regs r JOIN members m ON m.id=r.member_id "
                          f"WHERE r.event_id=? AND {PRESENT} ORDER BY m.id", (eid,)):
        try:
            lv = json.loads(r["answers"] or "{}").get(LEVEL_KEY)
        except ValueError:
            lv = None
        out.append((r["id"], r["code"], r["gender"], lv if lv in pairing.LEVELS else None))
    return out


def parse_rounds(form, ids):
    """form: r{n}_a_{i} / r{n}_b_{i} -> ([[(a,b|None)]], error). Each person at most once per round, ids must be eligible."""
    rounds = {}
    for k, v in form.multi_items() if hasattr(form, "multi_items") else form.items():
        m = re.fullmatch(r"r(\d)_([ab])_(\d{1,2})", k)
        if m:
            n, side, i = int(m[1]), m[2], int(m[3])
            if not 1 <= n <= pairing.MAX_ROUNDS:
                return None, "Jumlah ronde melebihi batas."
            rounds.setdefault(n, {}).setdefault(i, {})[side] = v.strip() if isinstance(v, str) else ""
    out = []
    for n in sorted(rounds):
        seen, pairs = set(), []
        for i in sorted(rounds[n]):
            a, b = rounds[n][i].get("a", ""), rounds[n][i].get("b", "")
            if not a and not b:
                continue
            try:
                a, b = int(a), (int(b) if b else None)
            except ValueError:
                return None, "ID peserta tidak valid."
            if a not in ids or (b is not None and b not in ids):
                return None, "Hanya peserta berstatus check-in yang boleh dipasangkan."
            if a == b:
                return None, f"Ronde {n}: tidak boleh memasangkan orang yang sama."
            for x in (a, b):
                if x is not None and x in seen:
                    return None, f"Ronde {n}: peserta muncul lebih dari sekali."
                seen.add(x)
            pairs.append((a, b))
        out.append(pairs)
    return (out, None) if out else (None, "Tidak ada ronde.")


def make_pick(conn, eid, chooser, chosen_code):
    """One atomic tx. Returns (status, connection_created). status: ok|self|unknown|closed|not_present"""
    with db.tx(conn):
        e = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not e or not event_ended(e):
            return "closed", False
        ok = lambda mid: conn.execute("SELECT 1 FROM event_regs r WHERE event_id=? AND member_id=? AND r.status='checked_in'", (eid, mid)).fetchone()
        if not ok(chooser):
            return "not_present", False
        t = conn.execute("SELECT id FROM members WHERE code=?", (chosen_code,)).fetchone()
        if not t or not ok(t["id"]):
            return "unknown", False   # same answer for nonexistent / other-event / not-checked-in: no oracle
        if t["id"] == chooser:
            return "self", False
        now = db.now_iso()
        conn.execute("INSERT OR IGNORE INTO event_picks(event_id,chooser_id,chosen_id,created_at) VALUES(?,?,?,?)", (eid, chooser, t["id"], now))
        made = False
        if conn.execute("SELECT 1 FROM event_picks WHERE event_id=? AND chooser_id=? AND chosen_id=?", (eid, t["id"], chooser)).fetchone():
            a, b = sorted((chooser, t["id"]))
            made = bool(conn.execute("INSERT OR IGNORE INTO event_connections(event_id,member_a,member_b,created_at) VALUES(?,?,?,?)",
                                     (eid, a, b, now)).rowcount)
            if made:
                db.audit(conn, None, "event_connection", f"event={eid} a={a} b={b}")
        return "ok", made


def mount(app, conn, jenv, secret_key, cookie_secure):
    def render(name, status=200, s=None, csrf="", **ctx):
        return HTMLResponse(jenv.get_template(name).render(sess=s, csrf=csrf, http_warning=not cookie_secure,
                                                           labels=db.ROLE_LABEL, **ctx), status_code=status)

    def staff(request):
        tok = request.cookies.get(auth.COOKIE)
        s = auth.load_session(conn, secret_key, tok) if tok else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        return (s, None) if s["roles"] & {"event", "owner"} else (None, PlainTextResponse("Forbidden", status_code=403))

    async def staff_post(request):
        s, bad = staff(request)
        if bad:
            return None, None, bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return None, None, PlainTextResponse("Forbidden", status_code=403)
        return s, form, None

    def page(s, e, rounds, msg=None, status=200, editable=False):
        ppl = participants(conn, e["id"])
        return render("event_pairs.html", status, s, s["csrf"], e=e, ppl=ppl, rounds=rounds, msg=msg, editable=editable,
                      max_rounds=pairing.MAX_ROUNDS, label={p[0]: p[1] for p in ppl})

    def saved(eid):
        rs = {}
        for r in conn.execute("SELECT round_no,member_a,member_b FROM event_pairings WHERE event_id=? ORDER BY round_no,id", (eid,)):
            rs.setdefault(r["round_no"], []).append((r["member_a"], r["member_b"]))
        return [rs[k] for k in sorted(rs)]

    def ev(eid):
        return conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()

    @app.get("/staff/event/{eid}/pasangan")
    async def pairs_view(request: Request, eid: int):
        s, bad = staff(request)
        if bad:
            return bad
        e = ev(eid)
        return page(s, e, saved(eid)) if e else PlainTextResponse("Not found", status_code=404)

    @app.post("/staff/event/{eid}/pasangan/pratinjau")
    async def pairs_preview(request: Request, eid: int):
        s, form, bad = await staff_post(request)
        if bad:
            return bad
        e = ev(eid)
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        n = form.get("rounds", "")
        if not (n.isascii() and n.isdigit() and 1 <= int(n) <= pairing.MAX_ROUNDS):
            return page(s, e, saved(eid), f"Jumlah ronde 1-{pairing.MAX_ROUNDS}.", 400)
        ppl = participants(conn, eid)
        try:
            rs = pairing.suggest([(p[0], p[2], p[3]) for p in ppl], int(n))
        except ValueError:
            return page(s, e, saved(eid), "Peserta terlalu banyak untuk saran otomatis (maks %d)." % pairing.MAX_PEOPLE, 400)
        return page(s, e, rs, "Pratinjau (belum disimpan). Ubah bila perlu lalu simpan.", editable=True)

    @app.post("/staff/event/{eid}/pasangan/simpan")
    async def pairs_save(request: Request, eid: int):
        s, form, bad = await staff_post(request)
        if bad:
            return bad
        e = ev(eid)
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        with db.tx(conn):   # eligibility is read inside the tx; replace-all makes a repeated save idempotent
            ids = {p[0] for p in participants(conn, eid)}
            rs, err = parse_rounds(form, ids)
            if not err:
                conn.execute("DELETE FROM event_pairings WHERE event_id=?", (eid,))
                now = db.now_iso()
                for n, pairs in enumerate(rs, 1):
                    for a, b in pairs:
                        conn.execute("INSERT INTO event_pairings(event_id,round_no,member_a,member_b,created_by,created_at) VALUES(?,?,?,?,?,?)",
                                     (eid, n, a, b, s["staff_id"], now))
                        # a person resting (b NULL) is stored as (a, NULL); a person in a pair appears once as member_a or member_b
                db.audit(conn, s["staff_id"], "event_pairings_save", f"event={eid} rounds={len(rs)}")
        if err:
            return page(s, e, rs or saved(eid), err, 400)
        return page(s, e, saved(eid), "Pasangan disimpan.")

    # ---- participant: post-event picks --------------------------------
    def msess(request):
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        s = auth.load_member_session(conn, secret_key, tok) if tok else None
        return s if s and s["member_id"] else None

    def picks_page(s, eid, msg=None, status=200):
        e = ev(eid)
        me = s["member_id"]
        if not e or not conn.execute("SELECT 1 FROM event_regs r WHERE event_id=? AND member_id=? AND r.status='checked_in'", (eid, me)).fetchone():
            return PlainTextResponse("Not found", status_code=404)   # not a checked-in participant: indistinguishable from unknown event
        ended = event_ended(e)
        cands = [p for p in participants(conn, eid) if p[0] != me] if ended else []
        mine = {r[0] for r in conn.execute("SELECT chosen_id FROM event_picks WHERE event_id=? AND chooser_id=?", (eid, me))}
        # contact is joined ONLY through rows where the viewer is a party: both picks exist by construction
        conns = conn.execute("SELECT m.code,m.full_name,m.phone FROM event_connections c JOIN members m "
                             "ON m.id=CASE WHEN c.member_a=? THEN c.member_b ELSE c.member_a END "
                             "WHERE c.event_id=? AND ? IN (c.member_a,c.member_b) ORDER BY c.id", (me, eid, me)).fetchall()
        r = render("event_picks.html", status, None, s["csrf"], e=e, ended=ended, cands=cands, mine=mine, conns=conns, msg=msg)
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.get("/akun/event/{eid}/pilihan")
    async def picks_view(request: Request, eid: str):
        s = msess(request)
        if not s:
            return RedirectResponse("/akun/masuk", 303)
        if not (eid.isascii() and eid.isdigit() and len(eid) < 10):
            return PlainTextResponse("Not found", status_code=404)
        return picks_page(s, int(eid))

    @app.post("/akun/event/pilihan")
    async def picks_submit(request: Request):
        s = msess(request)
        if not s:
            return RedirectResponse("/akun/masuk", 303)
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return PlainTextResponse("Forbidden", status_code=403)
        eid, code = form.get("event_id", ""), form.get("code", "")
        if not (isinstance(eid, str) and eid.isascii() and eid.isdigit() and len(eid) < 10 and isinstance(code, str) and len(code) <= 40):
            return PlainTextResponse("Bad request", status_code=400)
        st, made = make_pick(conn, int(eid), s["member_id"], code.strip())
        if st == "not_present":
            return PlainTextResponse("Not found", status_code=404)
        msgs = {"ok": "Pilihan tersimpan. Kontak dibuka hanya jika pilihan saling cocok." + (" Cocok! Lihat kontak di bawah." if made else ""),
                "closed": "Pilihan dibuka setelah event selesai.", "unknown": "Kode peserta tidak ditemukan.", "self": "Tidak bisa memilih diri sendiri."}
        return picks_page(s, int(eid), msgs[st], 200 if st == "ok" else 400)
