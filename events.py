"""Stage 5 chunk 1: event management (Event/Owner staff) + public /event/<slug> + member one-click registration.

Seats are reserved only when staff SELECT a registrant (BEGIN IMMEDIATE); no auto-expiry, release = explicit cancel.
Unknown (NULL) capacity / gender quota / price blocks selection (never treated as unlimited / free).
Payments, proof upload, check-in/QR and guest registration are later chunks.
"""
import json
import os
import re
import secrets
import io
import sqlite3
from datetime import date, datetime

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import segno

import auth
import db
import portal
import consent_evidence
import landing
import register

EVENT_STAFF = {"event", "owner"}
HELD = ("selected", "proof_uploaded", "paid", "checked_in")   # statuses that occupy a seat
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])")
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,29}")
FIELD_TYPES = ("choice", "text", "bool")
GENDER_COL = {"Laki-laki": "quota_m", "Perempuan": "quota_f"}


def _int(s, hi, blank_ok=True):
    s = (s or "").strip()
    if not s:
        return None, blank_ok
    if not (s.isascii() and s.isdigit() and len(s) <= 10 and int(s) <= hi):
        return None, False
    return int(s), True


def _dt(s):
    try:
        return datetime.strptime((s or "").strip().replace(" ", "T"), "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def parse_schema(text):
    """Bounded extra-question schema: list of {key,label,type,required,options?}. Returns (list|None, error|None)."""
    text = (text or "").strip()
    if not text:
        return [], None
    if len(text) > 4000:
        return None, "Pertanyaan tambahan terlalu panjang."
    try:
        raw = json.loads(text)
    except ValueError:
        return None, "Pertanyaan tambahan: JSON tidak valid."
    if not isinstance(raw, list) or len(raw) > 10:
        return None, "Pertanyaan tambahan: daftar maksimal 10 item."
    out, seen = [], set()
    for f in raw:
        if not isinstance(f, dict) or set(f) - {"key", "label", "type", "required", "options"}:
            return None, "Pertanyaan tambahan: item tidak valid."
        key, label, typ = f.get("key"), f.get("label"), f.get("type")
        if not (isinstance(key, str) and KEY_RE.fullmatch(key) and key not in seen):
            return None, "Pertanyaan tambahan: key harus unik a-z0-9_ (maks 30)."
        if not (isinstance(label, str) and 1 <= len(label.strip()) <= 100) or typ not in FIELD_TYPES:
            return None, "Pertanyaan tambahan: label/type tidak valid (choice/text/bool)."
        req = f.get("required", False)
        if not isinstance(req, bool):
            return None, "Pertanyaan tambahan: required harus true/false."
        item = {"key": key, "label": label.strip(), "type": typ, "required": req}
        if typ == "choice":
            o = f.get("options")
            if not (isinstance(o, list) and 2 <= len(o) <= 10 and all(isinstance(x, str) and 1 <= len(x.strip()) <= 60 for x in o)
                    and len({x.strip() for x in o}) == len(o)):
                return None, "Pertanyaan pilihan butuh 2-10 opsi unik (maks 60 karakter)."
            item["options"] = [x.strip() for x in o]
        elif "options" in f:
            return None, "Pertanyaan tambahan: options hanya untuk choice."
        seen.add(key)
        out.append(item)
    return out, None


def clean_answers(schema, form):
    """Validate member answers against the event schema. Returns (dict, error|None)."""
    ans = {}
    for f in schema:
        v = str(form.get("q_" + f["key"], "")).strip()
        if f["type"] == "bool":
            v = v == "ya"
            if f["required"] and not v:
                return None, f"Wajib dicentang: {f['label']}"
        elif not v:
            if f["required"]:
                return None, f"Wajib diisi: {f['label']}"
            v = ""
        elif f["type"] == "choice" and v not in f["options"]:
            return None, f"Pilihan tidak valid: {f['label']}"
        elif f["type"] == "text" and len(v) > 300:
            return None, f"Terlalu panjang (maks 300): {f['label']}"
        ans[f["key"]] = v
    return ans, None


def parse_event(form, conn, eid=None):
    """Returns (values dict, error). Pure validation; no fake cutoff/tier."""
    g = lambda k, n: str(form.get(k, "")).strip()[:n]   # noqa: E731
    title, slug = g("title", 121), g("slug", 61).lower()
    if not 1 <= len(title) <= 120:
        return None, "Judul 1-120 karakter."
    if not SLUG_RE.fullmatch(slug):
        return None, "Slug: 3-60 karakter a-z 0-9 dan tanda minus."
    if conn.execute("SELECT 1 FROM events WHERE slug=? AND id IS NOT ?", (slug, eid)).fetchone():
        return None, "Slug sudah dipakai."
    st, en = _dt(form.get("starts_at")), _dt(form.get("ends_at"))
    if not st or not en:
        return None, "Tanggal & jam mulai/selesai wajib (format valid)."
    if en <= st:
        return None, "Waktu selesai harus setelah mulai."
    price, ok1 = _int(form.get("price_idr"), 100_000_000)
    cap, ok2 = _int(form.get("capacity"), 100_000)
    qm, ok3 = _int(form.get("quota_m"), 100_000)
    qf, ok4 = _int(form.get("quota_f"), 100_000)
    if not (ok1 and ok2 and ok3 and ok4):
        return None, "Harga/kapasitas/kuota harus angka bulat >= 0 (kosong = belum ditetapkan)."
    if cap is not None and any(q is not None and q > cap for q in (qm, qf)):
        return None, "Kuota melebihi kapasitas."
    schema, err = parse_schema(form.get("extra_fields"))
    if err:
        return None, err
    v = dict(title=title, slug=slug, starts_at=st, ends_at=en, location=g("location", 200), dresscode=g("dresscode", 120),
             description=g("description", 2000), price_idr=price, capacity=cap, quota_m=qm, quota_f=qf,
             extra_fields=json.dumps(schema, ensure_ascii=False), active=1 if form.get("active") else 0)
    if eid is not None:   # never shrink below seats already held
        held = held_counts(conn, eid)
        for col, key, n in (("capacity", "total", cap), ("quota_m", "Laki-laki", qm), ("quota_f", "Perempuan", qf)):
            if n is not None and n < held[key]:
                return None, f"{col} tidak boleh di bawah kursi yang sudah terpakai ({held[key]})."
    return v, None


def held_counts(conn, eid):
    q = ("SELECT m.gender,COUNT(*) FROM event_regs r JOIN members m ON m.id=r.member_id "
         f"WHERE r.event_id=? AND r.status IN ({','.join('?' * len(HELD))}) GROUP BY m.gender")
    c = {"total": 0, "Laki-laki": 0, "Perempuan": 0}
    for g, n in conn.execute(q, (eid, *HELD)):
        c["total"] += n
        if g in c:
            c[g] = n
    return c


def select_seat(conn, eid, rid):
    """Atomic seat reservation. Returns (ok, message)."""
    with db.tx(conn):
        ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        r = conn.execute("SELECT r.*,m.gender FROM event_regs r JOIN members m ON m.id=r.member_id "
                         "WHERE r.id=? AND r.event_id=?", (rid, eid)).fetchone()
        if not ev or not r:
            return False, "Data tidak ditemukan."
        if r["status"] != "registered":
            return False, "Hanya pendaftar berstatus 'registered' yang bisa dipilih."
        if ev["price_idr"] is None:
            return False, "Harga event belum dikonfigurasi; seleksi/penagihan diblokir."
        if ev["capacity"] is None:
            return False, "Kapasitas belum dikonfigurasi (tidak dianggap tak terbatas)."
        col = GENDER_COL.get(r["gender"])
        if not col:
            return False, "Gender member belum terisi; tidak bisa dicek kuota."
        if ev[col] is None:
            return False, "Kuota gender belum dikonfigurasi (tidak dianggap tak terbatas)."
        held = held_counts(conn, eid)
        if held["total"] >= ev["capacity"]:
            return False, "Kapasitas penuh."
        if held[r["gender"]] >= ev[col]:
            return False, "Kuota gender penuh."
        conn.execute("UPDATE event_regs SET status='selected',selected_at=?,locked_price_idr=? WHERE id=?",
                     (db.now_iso(), ev["price_idr"], rid))
        return True, "Terpilih; kursi dipesan."


def mount(app, conn, jenv, secret_key, cookie_secure, fernet=None, priv_dir=None):
    def render(name, status=200, s=None, csrf="", **ctx):
        return HTMLResponse(jenv.get_template(name).render(sess=s, csrf=csrf, http_warning=not cookie_secure,
                                                           labels=db.ROLE_LABEL, **ctx), status_code=status)

    def gate(request):
        s = auth.load_session(conn, secret_key, request.cookies.get(auth.COOKIE) or "") if request.cookies.get(auth.COOKIE) else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        return (s, None) if s["roles"] & EVENT_STAFF else (None, PlainTextResponse("Forbidden", status_code=403))

    async def post_gate(request):
        s, bad = gate(request)
        if bad:
            return None, None, bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return None, None, PlainTextResponse("Forbidden", status_code=403)
        return s, form, None

    def ev_or_none(eid):
        return conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()

    def form_ctx(s, e, vals, error=None, status=200):
        return render("event_form.html", status, s, s["csrf"], e=e, v=vals, error=error)

    def detail(s, eid, msg=None, status=200):
        e = ev_or_none(eid)
        regs = conn.execute("SELECT r.*,m.code,m.gender FROM event_regs r JOIN members m ON m.id=r.member_id "
                            "WHERE r.event_id=? ORDER BY r.id", (eid,)).fetchall()
        schema = json.loads(e["extra_fields"] or "[]")
        rows = []
        for r in regs:
            a = json.loads(r["answers"] or "{}")
            rows.append((r, [(f["label"], ("ya" if a.get(f["key"]) is True else "tidak" if a.get(f["key"]) is False else a.get(f["key"], "")))
                             for f in schema]))
        return render("event_detail.html", status, s, s["csrf"], e=e, rows=rows, held=held_counts(conn, eid), msg=msg)

    @app.get("/staff/event")
    async def ev_list(request: Request):
        s, bad = gate(request)
        if bad:
            return bad
        rows = conn.execute("SELECT * FROM events ORDER BY COALESCE(starts_at,'9999') DESC,id DESC").fetchall()
        return render("event_list.html", 200, s, s["csrf"], events=rows)

    @app.get("/staff/event/baru")
    async def ev_new(request: Request):
        s, bad = gate(request)
        return bad or form_ctx(s, None, {"active": 1, "extra_fields": "[]"})

    @app.post("/staff/event/baru")
    async def ev_create(request: Request):
        s, form, bad = await post_gate(request)
        if bad:
            return bad
        v, err = parse_event(form, conn)
        if err:
            return form_ctx(s, None, dict(form) | {"active": form.get("active")}, err, 400)
        try:
            with db.tx(conn):
                cur = conn.execute(
                    "INSERT INTO events(title,slug,starts_at,ends_at,location,dresscode,description,price_idr,capacity,quota_m,quota_f,"
                    "extra_fields,active,created_at) VALUES(:title,:slug,:starts_at,:ends_at,:location,:dresscode,:description,"
                    ":price_idr,:capacity,:quota_m,:quota_f,:extra_fields,:active,:now)", v | {"now": db.now_iso()})
                db.audit(conn, s["staff_id"], "event_create", f"event={cur.lastrowid} slug={v['slug']}")
        except Exception as ex:   # race on UNIQUE(slug)
            if "UNIQUE" not in str(ex):
                raise
            return form_ctx(s, None, dict(form), "Slug sudah dipakai.", 400)
        return RedirectResponse(f"/staff/event/{cur.lastrowid}", 303)

    @app.get("/staff/event/{eid}")
    async def ev_detail(request: Request, eid: int):
        s, bad = gate(request)
        if bad:
            return bad
        return detail(s, eid) if ev_or_none(eid) else PlainTextResponse("Not found", status_code=404)

    @app.get("/staff/event/{eid}/ubah")
    async def ev_edit(request: Request, eid: int):
        s, bad = gate(request)
        if bad:
            return bad
        e = ev_or_none(eid)
        return form_ctx(s, e, dict(e)) if e else PlainTextResponse("Not found", status_code=404)

    @app.post("/staff/event/{eid}/ubah")
    async def ev_update(request: Request, eid: int):
        s, form, bad = await post_gate(request)
        if bad:
            return bad
        e = ev_or_none(eid)
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        try:
            with db.tx(conn):   # validation inside the tx: held counts cannot move between check and write
                v, err = parse_event(form, conn, eid)
                if not err:
                    conn.execute("UPDATE events SET title=:title,slug=:slug,starts_at=:starts_at,ends_at=:ends_at,location=:location,"
                                 "dresscode=:dresscode,description=:description,price_idr=:price_idr,capacity=:capacity,"
                                 "quota_m=:quota_m,quota_f=:quota_f,extra_fields=:extra_fields,active=:active WHERE id=:id",
                                 v | {"id": eid})
                    db.audit(conn, s["staff_id"], "event_update", f"event={eid}")
        except Exception as ex:
            if "UNIQUE" not in str(ex):
                raise
            err = "Slug sudah dipakai."
        if err:
            return form_ctx(s, e, dict(form), err, 400)
        return RedirectResponse(f"/staff/event/{eid}", 303)

    @app.post("/staff/event/{eid}/pendaftar/{rid}/pilih")
    async def ev_select(request: Request, eid: int, rid: int):
        s, form, bad = await post_gate(request)
        if bad:
            return bad
        if not ev_or_none(eid):
            return PlainTextResponse("Not found", status_code=404)
        ok, msg = select_seat(conn, eid, rid)
        if ok:
            db.audit(conn, s["staff_id"], "event_select", f"event={eid} reg={rid}")
        return detail(s, eid, msg, 200 if ok else 409)

    @app.post("/staff/event/{eid}/pendaftar/{rid}/batal")
    async def ev_cancel(request: Request, eid: int, rid: int):
        s, form, bad = await post_gate(request)
        if bad:
            return bad
        if not ev_or_none(eid):
            return PlainTextResponse("Not found", status_code=404)
        with db.tx(conn):
            r = conn.execute("SELECT status FROM event_regs WHERE id=? AND event_id=?", (rid, eid)).fetchone()
            ok = bool(r) and r["status"] in ("registered", "selected", "proof_uploaded")   # paid/checked_in: no refund automation; manual review only
            if ok:
                conn.execute("UPDATE event_regs SET status='cancelled',cancelled_at=? WHERE id=?", (db.now_iso(), rid))
                db.audit(conn, s["staff_id"], "event_cancel", f"event={eid} reg={rid} from={r['status']}")
        return detail(s, eid, "Dibatalkan; kursi dilepas." if ok else "Sudah dibayar/check-in: pembatalan finansial butuh kebijakan refund (belum dikonfigurasi); tinjau manual." if r and r["status"] in ("paid", "checked_in") else "Status ini tidak bisa dibatalkan.", 200 if ok else 409)

    # ---- public / member -------------------------------------------------
    def member_sess(request):
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        s = auth.load_member_session(conn, secret_key, tok) if tok else None
        return s if s and s["member_id"] else None

    def public(request, slug, status=200, msg=None, answers=None):
        e = conn.execute("SELECT * FROM events WHERE slug=? AND active=1", (slug,)).fetchone()
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        ms = member_sess(request)
        reg = conn.execute("SELECT id,status FROM event_regs WHERE event_id=? AND member_id=?",
                           (e["id"], ms["member_id"])).fetchone() if ms else None
        return render("public/event.html", status, None, ms["csrf"] if ms else "", e=e, schema=json.loads(e["extra_fields"] or "[]"),
                      logged=bool(ms), reg=reg, msg=msg, a=answers or {})

    @app.get("/event/{slug}")
    async def ev_public(request: Request, slug: str):
        return public(request, slug)

    @app.post("/event/{slug}/daftar")
    async def ev_register(request: Request, slug: str):
        ms = member_sess(request)
        if not ms:
            return RedirectResponse("/akun/masuk", 303)
        form = await request.form()
        if not auth.csrf_ok(ms, form.get("csrf_token")):
            return PlainTextResponse("Forbidden", status_code=403)
        e = conn.execute("SELECT * FROM events WHERE slug=? AND active=1", (slug,)).fetchone()
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        m = conn.execute("SELECT id,code,birth_date FROM members WHERE id=?", (ms["member_id"],)).fetchone()
        try:
            adult = db.is_adult(db.parse_birth_date(m["birth_date"]))   # full DOB required, no override
        except ValueError:
            adult = False
        if not adult:
            return public(request, slug, 403, f"Usia minimal {db.MIN_AGE} tahun (tanggal lahir lengkap diperlukan).")
        ans, err = clean_answers(json.loads(e["extra_fields"] or "[]"), form)
        if err:
            return public(request, slug, 400, err, dict(form))
        with db.tx(conn):
            cur = conn.execute("INSERT OR IGNORE INTO event_regs(event_id,member_id,status,created_at,answers) VALUES(?,?,'registered',?,?)",
                               (e["id"], m["id"], db.now_iso(), json.dumps(ans, ensure_ascii=False)))
            if cur.rowcount:
                db.audit(conn, None, "event_register", f"member={m['code']} event={e['id']}")
        return public(request, slug, 200, "Pendaftaran diterima. Staf akan memilih peserta." if cur.rowcount
                      else "Anda sudah terdaftar; data pendaftaran tidak diubah.")

    # ---- Stage 5 chunk 3: guest registration (normal participant member, NO subscription, registered != selected) ----
    GUEST_SLOTS = ("ktp_redacted", "photo_face")   # face only: no body photo for guests
    GUEST_STEP = ("ktp_nik_ok", "ktp_addr_ok", "ktp_sign_ok", "consent_terms", "consent_privacy", "consent_honest")

    def guest_clean(f):
        g = lambda k: f.get(k).strip() if isinstance(f.get(k), str) else ""
        c, e = {}, {}
        c["full_name"] = re.sub(r"\s+", " ", g("full_name"))
        if not 2 <= len(c["full_name"]) <= 100:
            e["full_name"] = "Nama lengkap 2-100 karakter."
        c["phone"] = register.normalize_phone(g("phone"))
        if not c["phone"]:
            e["phone"] = "Nomor HP tidak valid (contoh 0812xxxxxxxx)."
        c["email"] = g("email")
        if len(c["email"]) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c["email"]):
            e["email"] = "Email tidak valid."
        c["email_key"] = c["email"].casefold()
        pw = f.get("password") if isinstance(f.get("password"), str) else ""
        c["password"] = pw
        if not 12 <= len(pw) <= 256:
            e["password"] = "Password 12-256 karakter."
        if f.get("password2") != pw:
            e["password2"] = "Konfirmasi password tidak sama."
        c["gender"] = g("gender")
        if c["gender"] not in register.GENDER:
            e["gender"] = "Pilih gender."
        try:   # full DOB, hard 18+ gate, same helpers as /daftar
            born = db.parse_birth_date(g("birth_date"))
            if not db.is_adult(born):
                e["birth_date"] = f"Usia minimal {db.MIN_AGE} tahun."
            c["birth_date"] = born.isoformat()
        except ValueError as ex:
            e["birth_date"] = str(ex).capitalize() + "."
        for k in GUEST_STEP:
            if g(k) != "on":
                e[k] = "Wajib dicentang."
        return c, e

    def guest_page(request, slug, status=200):
        e = conn.execute("SELECT * FROM events WHERE slug=? AND active=1", (slug,)).fetchone()
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        s = auth.load_member_session(conn, secret_key, tok) if tok else None
        fresh = None
        if not s:
            t, csrf, ttl = auth.create_member_session(conn, secret_key)
            s, fresh = {"csrf": csrf}, (t, ttl)
        snap = consent_evidence.canonical_snapshot("guest")
        r = render("public/event_tamu.html", status, None, s["csrf"], e=e, schema=json.loads(e["extra_fields"] or "[]"),
                   gender=register.GENDER, max_mb=register.MAX_IMG // 1024 // 1024,
                   legal=landing.load_content()["legal"], consent_binding=consent_evidence.sign(snap, secret_key))
        if fresh:
            r.set_cookie(auth.MEMBER_COOKIE, fresh[0], max_age=fresh[1], httponly=True, samesite="lax", secure=cookie_secure, path="/")
        return r

    @app.get("/event/{slug}/tamu")
    async def ev_guest_form(request: Request, slug: str):
        return guest_page(request, slug)

    def gerr(errors, status=400):
        return JSONResponse({"ok": False, "errors": errors}, status_code=status)

    @app.post("/event/tamu/submit")
    async def ev_guest_submit(request: Request):
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        s = auth.load_member_session(conn, secret_key, tok) if tok else None
        if not auth.csrf_ok(s, request.headers.get("x-csrf-token") or ""):
            return JSONResponse({"ok": False, "error": "Sesi kedaluwarsa. Muat ulang halaman."}, status_code=403)
        if register._rate_limited(conn, register.client_ip(request)):
            return JSONResponse({"ok": False, "error": "Terlalu banyak percobaan. Coba lagi nanti."}, status_code=429)
        if fernet is None:
            return JSONResponse({"ok": False, "error": "Penyimpanan aman belum dikonfigurasi."}, status_code=503)
        try:
            form = await request.form(max_files=len(GUEST_SLOTS) + 2, max_fields=40)
        except Exception:
            return JSONResponse({"ok": False, "error": "Permintaan tidak valid."}, status_code=400)
        try:
            return await guest_register(s, form)
        finally:
            await form.close()

    async def guest_register(s, form):
        text = {k: v for k, v in form.items() if isinstance(v, str)}
        snap = consent_evidence.verify_binding(text.get("consent_binding"), "guest", secret_key)
        if snap is None or not consent_evidence.binding_matches_content(snap):
            # absent/tampered/stale/cross-flow: reject before any DB row or private-file write
            return JSONResponse({"ok": False,
                                 "errors": {"consent_terms": "Persetujuan tidak valid. Muat ulang halaman."}},
                                status_code=400)
        e = conn.execute("SELECT * FROM events WHERE slug=? AND active=1", (text.get("slug", ""),)).fetchone()
        if not e:
            return JSONResponse({"ok": False, "error": "Event tidak tersedia."}, status_code=404)
        clean, errors = guest_clean(text)
        ans, aerr = clean_answers(json.loads(e["extra_fields"] or "[]"), form)
        if aerr:
            errors["answers"] = aerr
        ups = {k: v for k, v in form.items() if not isinstance(v, str)}
        if any(k not in GUEST_SLOTS for k in ups):
            errors["ktp_redacted"] = "Hanya hasil sensor KTP dan foto wajah yang boleh dikirim."
        imgs = {}
        for slot in GUEST_SLOTS:
            up = ups.get(slot)
            if up is None:
                errors[slot] = "File wajib diunggah."
                continue
            try:
                imgs[slot] = register.clean_image(await up.read(register.MAX_IMG + 1), up.filename, up.content_type)
            except ValueError as ex:
                errors[slot] = str(ex)
        if errors:
            return gerr(errors)
        if conn.execute("SELECT 1 FROM members WHERE phone=? OR email_key=?", (clean["phone"], clean["email_key"])).fetchone():
            # never attach to / take over an existing account: tell them to log in
            return JSONResponse({"ok": False, "login": "/akun/masuk", "errors": {"phone": "Nomor HP atau email sudah terdaftar. Silakan masuk, lalu daftar event dari akun Anda."}}, status_code=409)
        portal.reconcile_guest(conn, priv_dir)   # bounded explicit retry of stale leftovers (no cron)
        with portal.guest_lock(priv_dir):   # SHARED from before the intent row until commit/cleanup is done (see portal.guest_lock)
            written = []
            try:
                stored = {}
                names = {slot: (secrets.token_hex(16) + (".enc" if slot == "ktp_redacted" else ".jpg")) for slot in imgs}
                with db.tx(conn):   # intent is COMMITTED before any byte is written: a hard crash can only leave a tracked row
                    for n in names.values():
                        conn.execute("INSERT INTO pending_guest_files(stored_path,created_at) VALUES(?,?)", (n, db.now_iso()))
                written = list(names.values())
                for slot, data in imgs.items():
                    enc = slot == "ktp_redacted"
                    register._write_private(priv_dir, fernet.encrypt(data) if enc else data, ".enc" if enc else ".jpg", names[slot])
                    stored[slot] = (names[slot], int(enc))
                pw_hash = auth.hash_password(clean["password"])
                now = db.now_iso()
                with db.tx(conn):   # member + files + event reg commit or roll back together
                    if not conn.execute("SELECT 1 FROM events WHERE id=? AND active=1", (e["id"],)).fetchone():
                        raise LookupError
                    code = db.next_member_code(conn)
                    mid = conn.execute(
                        "INSERT INTO members(code,full_name,birth_date,status,created_at,email,email_key,phone,password_hash,gender,consented_at,consent_text,consent_digest) "
                        "VALUES(?,?,?,'pending',?,?,?,?,?,?,?,?,?)",
                        (code, clean["full_name"], clean["birth_date"], now, clean["email"], clean["email_key"], clean["phone"],
                         pw_hash, clean["gender"], now,
                         json.dumps(snap, ensure_ascii=False, sort_keys=True), consent_evidence.digest(snap))).lastrowid
                    for slot, (name, enc) in stored.items():
                        conn.execute("INSERT INTO member_files(member_id,kind,stored_path,created_at,encrypted) VALUES(?,?,?,?,?)",
                                     (mid, slot, name, now, enc))
                        conn.execute("DELETE FROM pending_guest_files WHERE stored_path=?", (name,))   # now referenced
                    conn.execute("INSERT INTO event_regs(event_id,member_id,status,created_at,answers) VALUES(?,?,'registered',?,?)",
                                 (e["id"], mid, now, json.dumps(ans, ensure_ascii=False)))
                    db.audit(conn, None, "event_guest_register", f"member={code} event={e['id']}")
            except LookupError:
                _unlink(written)
                return JSONResponse({"ok": False, "error": "Event tidak tersedia."}, status_code=404)
            except sqlite3.IntegrityError:
                _unlink(written)
                if not conn.execute("SELECT 1 FROM members WHERE phone=? OR email_key=?", (clean["phone"], clean["email_key"])).fetchone():
                    return JSONResponse({"ok": False, "error": "Terjadi kesalahan di server. Coba lagi."}, status_code=500)
                return JSONResponse({"ok": False, "login": "/akun/masuk", "errors": {"phone": "Nomor HP atau email sudah terdaftar. Silakan masuk."}}, status_code=409)
            except Exception:
                _unlink(written)
                return JSONResponse({"ok": False, "error": "Terjadi kesalahan di server. Coba lagi."}, status_code=500)
        auth.drop_member_session(conn, s["token_hash"])
        t, _, ttl = auth.create_member_session(conn, secret_key, mid)
        resp = JSONResponse({"ok": True, "redirect": f"/event/{e['slug']}"})
        resp.set_cookie(auth.MEMBER_COOKIE, t, max_age=ttl, httponly=True, samesite="lax", secure=cookie_secure, path="/")
        return resp

    def _unlink(names):   # durable: failures stay queued in pending_guest_files for the next explicit retry
        portal.reconcile_guest(conn, priv_dir, names)

    # ---- Stage 5 chunk 2: payment proof (member) + finance verification ---------
    # Files follow the portal pattern: pending_file_cleanup row BEFORE the write, dropped in the tx that references
    # the file; replaced files get a cleanup row in the same tx; unlink only in reconcile (never inside a tx).
    FIN = {"finance", "owner"}

    def own_reg(s, rid):   # other members' / unknown ids look identical (404): no IDOR oracle
        if not (isinstance(rid, str) and rid.isascii() and rid.isdigit() and len(rid) < 10):
            return None
        return conn.execute("SELECT r.*,e.title,e.slug FROM event_regs r JOIN events e ON e.id=r.event_id "
                            "WHERE r.id=? AND r.member_id=?", (int(rid), s["member_id"])).fetchone()

    def invoice(s, r, status=200, msg=None):
        return render("event_invoice.html", status, None, s["csrf"], r=r, msg=msg, up=fernet is not None)

    @app.get("/akun/event/{rid}")
    async def ev_invoice(request: Request, rid: str):
        s = member_sess(request)
        if not s:
            return RedirectResponse("/akun/masuk", 303)
        r = own_reg(s, rid)
        return invoice(s, r) if r else PlainTextResponse("Not found", status_code=404)

    @app.post("/akun/event/bukti")
    async def ev_proof_upload(request: Request):
        s = member_sess(request)
        if not s:
            return RedirectResponse("/akun/masuk", 303)
        try:
            form = await request.form(max_files=2, max_fields=10)
        except Exception:
            return PlainTextResponse("Permintaan tidak valid", status_code=400)
        try:
            if not auth.csrf_ok(s, str(form.get("csrf_token") or "")):
                return PlainTextResponse("Forbidden", status_code=403)
            r = own_reg(s, form.get("rid"))
            if not r:
                return PlainTextResponse("Not found", status_code=404)
            if r["status"] != "selected" or r["locked_price_idr"] is None:
                return invoice(s, r, 409, "Bukti hanya bisa diunggah saat status 'selected' dengan harga terkunci.")
            if fernet is None:
                return invoice(s, r, 503, "Penyimpanan aman belum dikonfigurasi.")
            ups = {k: v for k, v in form.items() if not isinstance(v, str)}
            if set(ups) != {"proof"}:
                return invoice(s, r, 400, "Pilih satu file gambar bukti.")
            try:
                jpeg = register.clean_image(await ups["proof"].read(register.MAX_IMG + 1), ups["proof"].filename, ups["proof"].content_type)
            except ValueError as ex:
                return invoice(s, r, 400, str(ex))
            mid, name = s["member_id"], secrets.token_hex(16) + ".enc"
            try:
                conn.execute("INSERT INTO pending_file_cleanup(member_id,stored_path,reason,created_at) VALUES(?,?,'new',?)",
                             (mid, name, db.now_iso()))
                register._write_private(priv_dir, fernet.encrypt(jpeg), ".enc", name)
            except BaseException:
                portal.reconcile_pending(conn, priv_dir, mid)
                raise
            ok = False
            try:
                with db.tx(conn):
                    cur = conn.execute("SELECT status,proof_file_id FROM event_regs WHERE id=? AND member_id=?", (r["id"], mid)).fetchone()
                    if cur and cur["status"] == "selected":   # re-checked under the write lock
                        fid = conn.execute("INSERT INTO member_files(member_id,kind,stored_path,created_at,encrypted) VALUES(?,?,?,?,1)",
                                           (mid, "event_proof", name, db.now_iso())).lastrowid
                        conn.execute("DELETE FROM pending_file_cleanup WHERE stored_path=?", (name,))
                        if cur["proof_file_id"]:   # replacing a rejected proof
                            old = conn.execute("SELECT stored_path FROM member_files WHERE id=?", (cur["proof_file_id"],)).fetchone()
                            conn.execute("DELETE FROM member_files WHERE id=?", (cur["proof_file_id"],))
                            if old:
                                conn.execute("INSERT OR IGNORE INTO pending_file_cleanup(member_id,stored_path,reason,created_at) "
                                             "VALUES(?,?,'replaced',?)", (mid, old[0], db.now_iso()))
                        conn.execute("UPDATE event_regs SET status='proof_uploaded',proof_file_id=?,proof_uploaded_at=?,reject_reason=NULL "
                                     "WHERE id=?", (fid, db.now_iso(), r["id"]))
                        db.audit(conn, None, "event_proof_upload", f"reg={r['id']}")
                        ok = True
            finally:
                portal.reconcile_pending(conn, priv_dir, mid)   # also removes the new file if the tx rolled back / lost the race
            r = own_reg(s, str(r["id"]))
            return RedirectResponse(f"/akun/event/{r['id']}", 303) if ok else invoice(s, r, 409, "Status berubah; bukti tidak disimpan.")
        finally:
            await form.close()

    def fin_gate(request):
        c = request.cookies.get(auth.COOKIE)
        s = auth.load_session(conn, secret_key, c) if c else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        return (s, None) if s["roles"] & FIN else (None, PlainTextResponse("Forbidden", status_code=403))

    async def fin_post(request):
        s, bad = fin_gate(request)
        if bad:
            return None, None, bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return None, None, PlainTextResponse("Forbidden", status_code=403)
        return s, form, None

    def fin_queue(s, status=200, msg=None):
        rows = conn.execute("SELECT r.id,r.locked_price_idr,r.proof_uploaded_at,m.code,e.title FROM event_regs r "
                            "JOIN members m ON m.id=r.member_id JOIN events e ON e.id=r.event_id "
                            "WHERE r.status='proof_uploaded' ORDER BY r.proof_uploaded_at,r.id").fetchall()
        return render("event_finance.html", status, s, s["csrf"], rows=rows, msg=msg)

    @app.get("/keuangan/event")
    async def fin_ev_list(request: Request):
        s, bad = fin_gate(request)
        return bad or fin_queue(s)

    @app.get("/keuangan/event/{rid}/bukti")
    async def fin_ev_proof(request: Request, rid: int):
        s, bad = fin_gate(request)
        if bad:
            return bad
        r = conn.execute("SELECT f.stored_path,f.encrypted FROM event_regs g JOIN member_files f ON f.id=g.proof_file_id "
                         "WHERE g.id=? AND f.kind='event_proof'", (rid,)).fetchone()
        data = register.read_private(priv_dir, r["stored_path"], r["encrypted"], fernet) if r and fernet else None
        if data is None:
            return PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
        db.audit(conn, s["staff_id"], "event_proof_view", f"reg={rid}")   # id only, no PII
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/keuangan/event/{rid}/setuju")
    async def fin_ev_approve(request: Request, rid: int):
        s, form, bad = await fin_post(request)
        if bad:
            return bad
        with db.tx(conn):   # single winner: the status guard is part of the UPDATE
            n = conn.execute("UPDATE event_regs SET status='paid',paid_at=?,paid_by=? WHERE id=? AND status='proof_uploaded' "
                             "AND proof_file_id IS NOT NULL", (db.now_iso(), s["staff_id"], rid)).rowcount
            if n:
                db.audit(conn, s["staff_id"], "event_payment_approve", f"reg={rid}")
        return fin_queue(s, 200 if n else 409, "Pembayaran disetujui." if n else "Sudah diproses atau tidak ada bukti menunggu.")

    @app.post("/keuangan/event/{rid}/tolak")
    async def fin_ev_reject(request: Request, rid: int):
        s, form, bad = await fin_post(request)
        if bad:
            return bad
        reason = str(form.get("reason") or "").strip()
        if not 1 <= len(reason) <= 300:
            return fin_queue(s, 400, "Alasan tolak wajib (1-300 karakter).")
        with db.tx(conn):   # seat stays 'selected'; the rejected proof stays stored until the member replaces it
            n = conn.execute("UPDATE event_regs SET status='selected',reject_reason=? WHERE id=? AND status='proof_uploaded'",
                             (reason, rid)).rowcount
            if n:
                db.audit(conn, s["staff_id"], "event_payment_reject", f"reg={rid}")
        return fin_queue(s, 200 if n else 409, "Ditolak; member diminta unggah ulang." if n else "Sudah diproses.")

    # ---- Stage 5 chunk 3: QR ticket (member) + staff check-in ------------------
    # QR payload = random token only (192 bit, no PII). It is a check-in credential: shown only to the owner of a
    # paid/checked_in registration (no-store), accepted only via CSRF POST, never logged/audited/put in a URL.
    # ponytail: token stored plaintext so the ticket can be re-rendered; hash+re-issue if DB read-leak is in scope.
    NO_STORE = {"Cache-Control": "no-store"}

    def reg_token(r):
        tok = r["qr_token"]
        if not tok:
            with db.tx(conn):   # first writer wins; everyone re-reads the persisted value
                conn.execute("UPDATE event_regs SET qr_token=? WHERE id=? AND qr_token IS NULL AND status IN ('paid','checked_in')",
                             (secrets.token_urlsafe(24), r["id"]))
            tok = conn.execute("SELECT qr_token FROM event_regs WHERE id=?", (r["id"],)).fetchone()[0]
        return tok

    @app.get("/akun/event/{rid}/kode")   # manual fallback: only on explicit "Tampilkan", owner + paid/checked_in, no-store, never in a URL/log
    async def ev_code(request: Request, rid: str):
        s = member_sess(request)
        r = own_reg(s, rid) if s else None
        if not r or r["status"] not in ("paid", "checked_in"):
            return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
        return PlainTextResponse(reg_token(r), headers=NO_STORE)

    @app.get("/akun/event/{rid}/tiket.svg")
    async def ev_ticket(request: Request, rid: str):
        s = member_sess(request)
        if not s:
            return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
        r = own_reg(s, rid)
        if not r or r["status"] not in ("paid", "checked_in"):
            return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
        tok = reg_token(r)
        buf = io.BytesIO()
        segno.make(tok, error="m", micro=False).save(buf, kind="svg", scale=6, border=4, xmldecl=False, svgns=True)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers=NO_STORE)

    def scan_page(s, e, status=200, msg=None):
        return render("event_checkin.html", status, s, s["csrf"], e=e, msg=msg)

    @app.get("/staff/event/{eid}/checkin")
    async def ev_scan(request: Request, eid: int):
        s, bad = gate(request)
        if bad:
            return bad
        e = ev_or_none(eid)
        return scan_page(s, e) if e else PlainTextResponse("Not found", status_code=404)

    @app.post("/staff/event/{eid}/checkin")
    async def ev_checkin(request: Request, eid: int):
        s, form, bad = await post_gate(request)
        if bad:
            return bad
        e = ev_or_none(eid)
        if not e:
            return PlainTextResponse("Not found", status_code=404)
        tok = str(form.get("token") or "").strip()
        code, msg = 400, "Token tidak valid."
        name, n = None, 0
        if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", tok):
            with db.tx(conn):   # single winner: the status/event guard is part of the UPDATE
                n = conn.execute("UPDATE event_regs SET status='checked_in',checked_in_at=?,checked_in_by=? WHERE qr_token=? "
                                 "AND event_id=? AND status='paid' AND EXISTS(SELECT 1 FROM events WHERE id=? AND active=1)",
                                 (db.now_iso(), s["staff_id"], tok, eid, eid)).rowcount
                r = conn.execute("SELECT r.id,r.event_id,r.status,m.code FROM event_regs r JOIN members m ON m.id=r.member_id "
                                 "WHERE r.qr_token=?", (tok,)).fetchone()
                if n:
                    db.audit(conn, s["staff_id"], "event_checkin", f"event={eid} reg={r['id']}")   # ids only, never the token
            if n:
                code, msg, name = 200, "Check-in berhasil.", r["code"]
            elif r and r["event_id"] == eid and r["status"] == "checked_in":
                code, msg, name = 200, "Sudah check-in sebelumnya (tidak dihitung ulang).", r["code"]
            elif r and r["event_id"] != eid:
                code, msg = 409, "Tiket untuk event lain."
            elif r and not e["active"]:
                code, msg = 409, "Event tidak aktif."
            elif r:
                code, msg = 409, "Tiket belum lunas / tidak dapat dipakai."
        already = code == 200 and not n
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": code == 200, "already": already, "message": msg, "member": name},
                                status_code=code, headers=NO_STORE)
        return scan_page(s, e, code, msg + (f" ({name})" if name else ""))
