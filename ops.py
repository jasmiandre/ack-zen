"""Stage 4 ops: staff WhatsApp click-links (never sent by the app), Silver internal card, Owner CSV import (dry-run first) and CSV export."""
import csv
import io
import re
import textwrap
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.responses import Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import auth
import db
import register

TPL_ROLES = {"welcome": {"matchmaker", "owner"}, "bayar_info": {"finance", "owner"}, "bayar_pengingat": {"finance", "owner"},
             "verifikasi_tolak": {"verifier", "owner"}, "kandidat": {"matchmaker", "owner"}, "event_pengingat": {"event", "owner"}}
CARD_ROLES = {"matchmaker", "owner"}
OWNER = {"owner"}
SILVER, PRIVATE = "ACK Silver", "ACK Private Concierge"   # only Silver is published; Private never; other programs not assumed
IMPORT_MAX_BYTES, IMPORT_MAX_ROWS = 256 * 1024, 500
FIELDS = (("full_name", "Nama"), ("birth_date", "Tanggal lahir (YYYY-MM-DD)"), ("phone", "HP"), ("email", "Email"))
EXPORT_COLS = ("code", "full_name", "phone", "email", "status", "pipeline", "gender", "domicile", "religion", "ethnicity", "created_at")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
CTRL = re.compile(r"[\x00-\x20\x7f]")


def csv_safe(v):
    """Neutralise spreadsheet formulas: a cell whose first non-space/control char is = + - @ gets a leading apostrophe."""
    s = "" if v is None else str(v)
    t = CTRL.sub("", s[:8]) if s else ""
    return "'" + s if t[:1] in ("=", "+", "-", "@") or s[:1] in ("\t", "\r") else s


def wa_link(phone, body, code):
    return f"https://wa.me/{phone}?text={quote(body.replace('{kode}', code), safe='')}"


def parse_rows(text, mapping):
    """-> list of (line, clean|None, error|None). No values in errors (no PII)."""
    rd = csv.DictReader(io.StringIO(text))
    if rd.fieldnames is None or any(mapping[k] not in rd.fieldnames for k, _ in FIELDS):
        raise ValueError("Kolom hasil pemetaan tidak ada di header CSV.")
    out, seen_p, seen_e = [], set(), set()
    for i, row in enumerate(rd):
        if i >= IMPORT_MAX_ROWS:
            raise ValueError(f"Maksimal {IMPORT_MAX_ROWS} baris.")
        g = lambda k: (row.get(mapping[k]) or "").strip()
        name = " ".join(g("full_name").split())
        phone, email = register.normalize_phone(g("phone")), g("email")[:254].casefold()
        err = None
        try:
            born = db.parse_birth_date(g("birth_date"))
        except ValueError:
            born = None
            err = "tanggal lahir kosong/tidak lengkap"
        if err is None and not db.is_adult(born):
            err = "usia di bawah 18"
        elif err is None and not 1 <= len(name) <= 100:
            err = "nama kosong/terlalu panjang"
        elif err is None and not phone:
            err = "HP tidak valid"
        elif err is None and not EMAIL_RE.fullmatch(email):
            err = "email tidak valid"
        elif err is None and (phone in seen_p or email in seen_e):
            err = "duplikat di file"
        if err is None:
            seen_p.add(phone); seen_e.add(email)
        out.append((i + 2, None if err else dict(full_name=name, birth_date=born.isoformat(), phone=phone, email=email, email_key=email), err))
    return out


def mount(app, conn, jenv, secret_key, cookie_secure, fernet, priv_dir):
    def gate(request, roles):
        tok = request.cookies.get(auth.COOKIE)
        s = auth.load_session(conn, secret_key, tok) if tok else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        return (s, None) if s["roles"] & roles else (None, PlainTextResponse("Forbidden", status_code=403))

    async def gate_post(request, roles):
        s, bad = gate(request, roles)
        if bad:
            return None, None, bad
        form = await request.form(max_files=1, max_fields=20)
        if not auth.csrf_ok(s, form.get("csrf_token")):
            await form.close()
            return None, None, PlainTextResponse("Forbidden", status_code=403)
        return s, form, None

    def page(name, s, status=200, **ctx):
        return HTMLResponse(jenv.get_template(name).render(sess=s, csrf=s["csrf"], http_warning=not cookie_secure,
                                                           labels=db.ROLE_LABEL, **ctx), status_code=status)

    # ---- WhatsApp: links only; staff clicks, app never sends. Templates carry member code only (no name/contact of anyone else) ----
    @app.get("/wa/{mid}")
    async def wa(request: Request, mid: int):
        s, bad = gate(request, {r for rs in TPL_ROLES.values() for r in rs})
        if bad:
            return bad
        m = conn.execute("SELECT code,phone FROM members WHERE id=?", (mid,)).fetchone()
        if not m:
            return PlainTextResponse("Not found", status_code=404)
        tpls = [(r["key"], wa_link(m["phone"], r["body"], m["code"]) if m["phone"] else None)
                for r in conn.execute("SELECT key,body FROM wa_templates ORDER BY id") if TPL_ROLES.get(r["key"], set()) & s["roles"]]
        db.audit(conn, s["staff_id"], "wa_page_view", f"member={mid}")
        return page("wa.html", s, code=m["code"], tpls=tpls)

    # ---- Silver card: internal download only; consent + program gate; Private Concierge never ----
    def card_png(m, photo, blur):
        im = Image.new("RGB", (1080, 1350), (236, 236, 240))
        d = ImageDraw.Draw(im)
        f = lambda n: ImageFont.load_default(size=n)
        if photo:
            try:
                p = Image.open(io.BytesIO(photo)).convert("RGB")
                p.thumbnail((960, 700))
                if blur:
                    p = p.filter(ImageFilter.GaussianBlur(max(p.size) // 12))
                im.paste(p, (60 + (960 - p.width) // 2, 60))
            except Exception:   # noqa: BLE001 - unreadable photo: placeholder, still no data loss
                photo = None
        if not photo:
            d.rectangle((60, 60, 1020, 760), fill=(200, 200, 208))
        d.text((60, 800), m["code"], font=f(64), fill=(20, 20, 30))
        y = 890
        for lbl, v in (("Usia", m["age"]), ("Domisili", m["domicile"]), ("Pekerjaan", m["occupation"]), ("Agama", m["religion"])):
            d.text((60, y), f"{lbl}: {v or '-'}", font=f(40), fill=(40, 40, 50)); y += 60
        for line in textwrap.wrap(m["intro"] or "", 55)[:6]:
            d.text((60, y + 10), line, font=f(34), fill=(60, 60, 70)); y += 46
        out = io.BytesIO(); im.save(out, "PNG")
        return out.getvalue()

    @app.get("/kartu/{mid}")
    async def card(request: Request, mid: int):
        s, bad = gate(request, CARD_ROLES)
        if bad:
            return bad
        m = conn.execute("SELECT id,code,birth_date,domicile,occupation,religion,intro,publish_consent,status FROM members WHERE id=?", (mid,)).fetchone()
        if not m:
            return PlainTextResponse("Not found", status_code=404)
        progs = {r[0] for r in conn.execute("SELECT p.name FROM subscriptions su JOIN programs p ON p.id=su.program_id "
                                            "WHERE su.member_id=? AND su.status IN ('pending','active')", (mid,))}
        if PRIVATE in progs or SILVER not in progs:
            return PlainTextResponse("Kartu hanya untuk member ACK Silver.", status_code=409)
        if m["status"] != "verified" or m["publish_consent"] not in ("ya", "ya_blur"):
            return PlainTextResponse("Tidak ada persetujuan publikasi / belum terverifikasi.", status_code=409)
        f = conn.execute("SELECT stored_path,encrypted FROM member_files WHERE member_id=? AND kind='photo_face' ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
        photo = register.read_private(priv_dir, f["stored_path"], f["encrypted"], fernet) if f and (fernet or not f["encrypted"]) else None
        age = db.age_on(db.parse_birth_date(m["birth_date"]), db.date.today())
        png = card_png(dict(m, age=age), photo, m["publish_consent"] == "ya_blur")
        db.audit(conn, s["staff_id"], "card_download", f"member={mid} blur={int(m['publish_consent'] == 'ya_blur')}")
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="{m["code"]}-card.png"'})

    # ---- Owner CSV import: preview (dry-run) never writes; commit requires explicit action + re-upload ----
    def import_page(s, status=200, **ctx):
        return page("import.html", s, status, fields=FIELDS, max_rows=IMPORT_MAX_ROWS, **ctx)

    @app.get("/owner/impor")
    async def import_get(request: Request):
        s, bad = gate(request, OWNER)
        return bad or import_page(s)

    @app.post("/owner/impor")
    async def import_post(request: Request):
        s, form, bad = await gate_post(request, OWNER)
        if bad:
            return bad
        up = form.get("file")
        try:
            raw = await up.read(IMPORT_MAX_BYTES + 1) if hasattr(up, "read") else b""
        finally:
            await form.close()
        mapping = {k: str(form.get("map_" + k) or "").strip() for k, _ in FIELDS}
        try:
            if not raw or len(raw) > IMPORT_MAX_BYTES or not all(mapping.values()):
                raise ValueError("File wajib (maks 256 KB) dan semua pemetaan kolom harus diisi.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("File harus UTF-8.")
            rows = parse_rows(text, mapping)
        except (ValueError, csv.Error) as e:
            return import_page(s, 400, error=str(e))
        phones = {r[1]["phone"] for r in rows if r[1]}
        emails = {r[1]["email_key"] for r in rows if r[1]}
        res, ok = [], 0
        commit = form.get("action") == "commit"
        with db.tx(conn) if commit else conn.lock:   # dry-run: no write transaction at all
            for line, c, err in rows:
                if c and (conn.execute("SELECT 1 FROM members WHERE phone=? OR email_key=?", (c["phone"], c["email_key"])).fetchone()):
                    c, err = None, "sudah terdaftar (HP/email)"
                if c and commit:
                    conn.execute("INSERT INTO members(code,full_name,birth_date,status,created_at,email,email_key,phone,perlu_review,source) "
                                 "VALUES(?,?,?,'pending',?,?,?,?,1,'import')",
                                 (db.next_member_code(conn), c["full_name"], c["birth_date"], db.now_iso(), c["email"], c["email_key"], c["phone"]))
                ok += bool(c)
                res.append((line, "OK" if c else "DITOLAK", err or ""))
            if commit:
                db.audit(conn, s["staff_id"], "member_import", f"accepted={ok} rejected={len(res) - ok}")
        return import_page(s, committed=commit, res=res, ok=ok)

    # ---- Owner CSV export: POST+CSRF (audited); no password/key/file paths columns exist in EXPORT_COLS ----
    @app.post("/owner/ekspor")
    async def export(request: Request):
        s, form, bad = await gate_post(request, OWNER)
        if bad:
            return bad
        await form.close()
        with db.tx(conn):
            rows = conn.execute(f"SELECT {','.join(EXPORT_COLS)} FROM members ORDER BY id").fetchall()
            db.audit(conn, s["staff_id"], "member_export", f"rows={len(rows)}")
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(EXPORT_COLS)
        for r in rows:
            w.writerow([csv_safe(r[c]) for c in EXPORT_COLS])
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="members.csv"'})
