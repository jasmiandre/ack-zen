"""ACK Stage 2: member registration (/daftar), member session pages (/akun), private image handling.
Dummy data only. Nothing here logs form values; audit rows carry the member code only."""
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from io import BytesIO

from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image, ImageOps

import auth
import db
import landing

log = logging.getLogger("ack.register")

MAX_IMG = 10 * 1024 * 1024            # per file, as received
MAX_PIXELS = 40_000_000               # decompression-bomb ceiling (checked from the header, before decoding)
MAX_SIDE = 1600                       # server-side resize
BODY_LIMIT = 3 * MAX_IMG + 512 * 1024  # whole multipart request
SUBMIT_LIMIT, SUBMIT_WINDOW = 10, 3600  # POST /daftar/submit per client IP per hour
SLOTS = ("ktp_redacted", "photo_body", "photo_face")
FILE_RE = re.compile(r"[0-9a-f]{32}\.(enc|jpg)")
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

GENDER = ("Laki-laki", "Perempuan")
ETHNICITY = ("Chinese Indonesia", "Local Indonesia", "WNA Asian", "WNA Western", "Lainnya")
RELIGION = ("Islam", "Kristen", "Katolik", "Hindu", "Buddha", "Konghucu", "Lainnya")
MARITAL = ("Single", "Cerai Mati (tanpa anak)", "Cerai Mati (dengan anak)", "Cerai Hidup (tanpa anak)",
           "Cerai Hidup (dengan anak)", "Belum Resmi Bercerai", "Menikah/Tunangan/Pacaran")
REVIEW_STATUS = ("Belum Resmi Bercerai", "Menikah/Tunangan/Pacaran")
EDUCATION = ("SMA/SMK", "D3", "S1", "S2", "S3", "Lainnya")
PUBLISH = {"ya": "Ya", "tidak": "Tidak", "ya_blur": "Ya, dengan wajah diblur"}
SOURCE = ("Instagram", "TikTok", "WhatsApp", "Teman/Keluarga", "Event", "Lainnya")
PREF_RELIGION = ("Bebas",) + RELIGION
STEP_OF = {"full_name": 1, "phone": 1, "email": 1, "password": 1, "password2": 1,
           "gender": 2, "birth_date": 2, "domicile": 2, "ethnicity": 2, "marga": 2, "religion": 2,
           "marital_status": 2, "education": 2, "height_cm": 2,
           "intro": 3, "pref_age_min": 3, "pref_age_max": 3, "pref_religion": 3, "pref_domicile": 3,
           "pref_notes": 3, "publish_consent": 3, "source": 3,
           "ktp_redacted": 4, "photo_body": 4, "photo_face": 4, "ktp_nik_ok": 4, "ktp_addr_ok": 4, "ktp_sign_ok": 4,
           "program_id": 5, "consent_terms": 5, "consent_privacy": 5, "consent_honest": 5}


def normalize_phone(raw):
    """Indonesian mobile -> 628xxxxxxxxx (digits only), or None."""
    d = re.sub(r"[\s().+-]", "", raw or "")
    if not d.isdigit():
        return None
    d = "62" + d[1:] if d.startswith("0") else d
    d = "62" + d if d.startswith("8") else d
    return d if re.fullmatch(r"628\d{8,11}", d) else None


def validate(f, today=None):
    """Field rules for steps 1-3 and 5 (+ KTP checklist). Returns (clean, {field: msg}, first_bad_step)."""
    e, c = {}, {}
    g = lambda k: (f.get(k) or "").strip() if isinstance(f.get(k), (str, type(None))) else ""

    def need(k, ok, msg):
        if not ok:
            e[k] = msg

    c["full_name"] = re.sub(r"\s+", " ", g("full_name"))
    need("full_name", 2 <= len(c["full_name"]) <= 100, "Nama lengkap 2-100 karakter.")
    c["phone"] = normalize_phone(g("phone"))
    need("phone", c["phone"], "Nomor HP tidak valid (contoh 0812xxxxxxxx).")
    c["email"] = g("email")
    need("email", len(c["email"]) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c["email"]), "Email tidak valid.")
    c["email_key"] = c["email"].casefold()
    pw = f.get("password") if isinstance(f.get("password"), str) else ""
    c["password"] = pw
    need("password", 12 <= len(pw) <= 256, "Password 12-256 karakter.")
    need("password2", f.get("password2") == pw, "Konfirmasi password tidak sama.")

    for k, opts, msg in (("gender", GENDER, "Pilih gender."), ("ethnicity", ETHNICITY, "Pilih suku."),
                         ("religion", RELIGION, "Pilih agama."), ("marital_status", MARITAL, "Pilih status."),
                         ("education", EDUCATION, "Pilih pendidikan."), ("source", SOURCE, "Pilih salah satu."),
                         ("pref_religion", PREF_RELIGION, "Pilih agama pasangan.")):
        c[k] = g(k)
        need(k, c[k] in opts, msg)
    c["publish_consent"] = g("publish_consent")
    need("publish_consent", c["publish_consent"] in PUBLISH, "Pilih salah satu.")
    try:  # full date, hard 18+ gate: no override path exists anywhere
        born = db.parse_birth_date(g("birth_date"))
        if not db.is_adult(born, today):
            e["birth_date"] = f"Usia minimal {db.MIN_AGE} tahun."
        c["birth_date"] = born.isoformat()
    except ValueError as ex:
        e["birth_date"] = str(ex).capitalize() + "."
    for k, lim in (("domicile", 100), ("marga", 60), ("pref_domicile", 100), ("pref_notes", 500)):
        c[k] = g(k)
        need(k, len(c[k]) <= lim, f"Maksimal {lim} karakter.")
    need("domicile", c["domicile"], "Isi domisili.")
    need("pref_domicile", c["pref_domicile"], "Isi domisili pasangan (boleh 'Bebas').")
    c["intro"] = g("intro")
    need("intro", 30 <= len(c["intro"]) <= 2000, "Perkenalan minimal 30 karakter (maks 2000).")

    def num(k, lo, hi, msg):
        try:
            c[k] = int(g(k))
        except ValueError:
            c[k] = None
        need(k, c[k] is not None and lo <= c[k] <= hi, msg)
    num("height_cm", 100, 250, "Tinggi badan 100-250 cm.")
    num("pref_age_min", db.MIN_AGE, 100, f"Usia minimal {db.MIN_AGE}-100.")
    num("pref_age_max", db.MIN_AGE, 100, f"Usia maksimal {db.MIN_AGE}-100.")
    if "pref_age_min" not in e and "pref_age_max" not in e and c["pref_age_min"] > c["pref_age_max"]:
        e["pref_age_max"] = "Rentang usia terbalik."
    try:
        c["program_id"] = int(g("program_id"))
    except ValueError:
        e["program_id"] = "Pilih program."
    for k, msg in (("ktp_nik_ok", "Konfirmasi NIK sudah tertutup."), ("ktp_addr_ok", "Konfirmasi alamat sudah tertutup."),
                   ("ktp_sign_ok", "Konfirmasi tanda tangan sudah tertutup."),
                   ("consent_terms", "Setujui syarat & ketentuan."), ("consent_privacy", "Setujui kebijakan privasi."),
                   ("consent_honest", "Pernyataan status jujur wajib.")):
        need(k, g(k) == "on", msg)
    c["perlu_review"] = int(c["marital_status"] in REVIEW_STATUS)
    return c, e, (min(STEP_OF[k] for k in e) if e else 0)


def clean_image(raw):
    """Decode (jpg/png/webp only), apply EXIF orientation, drop all metadata, resize, re-encode as JPEG."""
    if not raw or len(raw) > MAX_IMG:
        raise ValueError("Ukuran file maksimal 10 MB.")
    try:
        im = Image.open(BytesIO(raw))
        if im.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError
        if im.width * im.height > MAX_PIXELS:
            raise ValueError
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            im = Image.new("RGB", rgba.size, "white")
            im.paste(rgba, mask=rgba.getchannel("A"))
        else:
            im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = BytesIO()
        im.save(out, "JPEG", quality=88)  # no exif= argument: metadata is not carried over
        return out.getvalue()
    except Exception:
        raise ValueError("File harus gambar JPG/PNG/WEBP yang valid (maks 10 MB).") from None


class BodyLimit:
    """Counts bytes actually received for one path, so a missing/lying Content-Length cannot bypass the cap."""

    def __init__(self, app, path, limit):
        self.app, self.path, self.limit = app, path, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.path or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        started = []
        for k, v in scope["headers"]:
            if k == b"content-length" and v.isdigit() and int(v) > self.limit:
                return await self._reject(send)
        got = 0

        async def rcv():
            nonlocal got
            m = await receive()
            if m["type"] == "http.request":
                got += len(m.get("body", b""))
                if got > self.limit:
                    raise OverflowError
            return m

        async def snd(m):
            if m["type"] == "http.response.start":
                started.append(1)
            await send(m)
        try:
            await self.app(scope, rcv, snd)
        except OverflowError:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": b'{"ok":false,"error":"Ukuran unggahan terlalu besar."}'})


def client_ip(request):
    ip = request.client.host if request.client else "?"
    xff = request.headers.get("x-forwarded-for")
    if xff and ip in ("127.0.0.1", "::1"):  # trust the header only from the local reverse proxy
        ip = xff.split(",")[-1].strip()[:45]
    return ip


def _rate_limited(conn, ip):
    now = int(time.time())
    conn.execute("DELETE FROM reg_attempts WHERE ts<?", (now - SUBMIT_WINDOW,))
    if conn.execute("SELECT COUNT(*) FROM reg_attempts WHERE ip=?", (ip,)).fetchone()[0] >= SUBMIT_LIMIT:
        return True
    conn.execute("INSERT INTO reg_attempts(ts,ip) VALUES(?,?)", (now, ip))
    return False


def _write_private(directory, data, ext):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    name = secrets.token_hex(16) + ext  # random; the client filename is never used
    fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return name


def read_private(directory, stored, encrypted, fernet):
    if not FILE_RE.fullmatch(stored or ""):
        return None
    try:
        with open(os.path.join(directory, stored), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return fernet.decrypt(data) if encrypted else data


def mount(app, conn, jenv, secret_key, cookie_secure, fernet_key, priv_dir):
    fernet = Fernet(fernet_key.encode()) if fernet_key else None
    os.makedirs(priv_dir, mode=0o700, exist_ok=True)

    def current(request):
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        return auth.load_member_session(conn, secret_key, tok) if tok else None

    def set_cookie(resp, tok, ttl):
        resp.set_cookie(auth.MEMBER_COOKIE, tok, max_age=ttl, httponly=True, samesite="lax",
                        secure=cookie_secure, path="/")

    def render(name, resp_cls=HTMLResponse, status=200, sess=None, **ctx):
        return resp_cls(jenv.get_template(name).render(csrf=(sess or {}).get("csrf", ""),
                                                       http_warning=not cookie_secure, **ctx), status_code=status)

    def with_session(request, name, **ctx):
        """Render with the visitor's session, minting an anonymous one (CSRF carrier) when absent."""
        s = current(request)
        fresh = None
        if not s:
            tok, csrf, ttl = auth.create_member_session(conn, secret_key)
            s, fresh = {"csrf": csrf, "member_id": None}, (tok, ttl)
        resp = render(name, sess=s, **ctx)
        if fresh:
            set_cookie(resp, *fresh)
        return resp

    def csrf_ok(request, s):
        return auth.csrf_ok(s, request.headers.get("x-csrf-token") or "")

    def err(errors, status=400):
        return JSONResponse({"ok": False, "errors": errors,
                             "step": min(STEP_OF.get(k, 1) for k in errors)}, status_code=status)

    @app.get("/daftar")
    async def form(request: Request):
        progs = conn.execute("SELECT id,name,description,benefits,price_idr FROM programs WHERE active=1 "
                             "ORDER BY sort_order").fetchall()
        return with_session(request, "public/daftar.html", progs=progs, price=db.price_label, legal=landing.load_content()["legal"],
                            o=dict(gender=GENDER, ethnicity=ETHNICITY, religion=RELIGION, marital=MARITAL,
                                   education=EDUCATION, publish=PUBLISH, source=SOURCE, pref_religion=PREF_RELIGION),
                            max_mb=MAX_IMG // 1024 // 1024)

    @app.post("/daftar/submit")
    async def submit(request: Request):
        s = current(request)
        if not csrf_ok(request, s):
            return JSONResponse({"ok": False, "error": "Sesi kedaluwarsa. Muat ulang halaman."}, status_code=403)
        if _rate_limited(conn, client_ip(request)):
            return JSONResponse({"ok": False, "error": "Terlalu banyak percobaan. Coba lagi nanti."}, status_code=429)
        if fernet is None:
            return JSONResponse({"ok": False, "error": "Penyimpanan aman belum dikonfigurasi."}, status_code=503)
        try:
            form = await request.form(max_files=len(SLOTS) + 2, max_fields=60)
        except Exception:
            return JSONResponse({"ok": False, "error": "Permintaan tidak valid."}, status_code=400)
        try:
            return await _register(request, form, s)
        finally:
            await form.close()

    async def _register(request, form, s):
        text = {k: v for k, v in form.items() if isinstance(v, str)}
        clean, errors, _ = validate(text)
        ups = {k: v for k, v in form.items() if not isinstance(v, str)}
        for k in ups:
            if k not in SLOTS:
                errors["ktp_redacted"] = "Hanya hasil sensor KTP yang boleh dikirim."  # e.g. an original 'ktp' field
        imgs = {}
        for slot in SLOTS:
            up = ups.get(slot)
            if up is None:
                errors[slot] = "File wajib diunggah."
                continue
            try:
                imgs[slot] = clean_image(await up.read(MAX_IMG + 1))
            except ValueError as ex:
                errors[slot] = str(ex)
        if errors:
            return err(errors)
        prog = conn.execute("SELECT id,price_idr FROM programs WHERE id=? AND active=1", (clean["program_id"],)).fetchone()
        if not prog:
            return err({"program_id": "Program tidak tersedia."})
        if conn.execute("SELECT 1 FROM members WHERE phone=?", (clean["phone"],)).fetchone():
            return err({"phone": "Nomor HP sudah terdaftar."}, 409)
        if conn.execute("SELECT 1 FROM members WHERE email_key=?", (clean["email_key"],)).fetchone():
            return err({"email": "Email sudah terdaftar."}, 409)

        written = []
        try:
            stored = {}
            for slot, data in imgs.items():
                enc = slot == "ktp_redacted"
                name = _write_private(priv_dir, fernet.encrypt(data) if enc else data, ".enc" if enc else ".jpg")
                written.append(name)
                stored[slot] = (name, int(enc))
            pw_hash = auth.hash_password(clean["password"])
            now = db.now_iso()
            with db.tx(conn):
                code = db.next_member_code(conn)
                mid = conn.execute(
                    "INSERT INTO members(code,full_name,birth_date,status,created_at,email,email_key,phone,password_hash,"
                    "gender,domicile,ethnicity,marga,religion,marital_status,education,height_cm,intro,pref_age_min,"
                    "pref_age_max,pref_religion,pref_domicile,pref_notes,publish_consent,source,perlu_review,consented_at) "
                    "VALUES(?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, clean["full_name"], clean["birth_date"], now, clean["email"], clean["email_key"], clean["phone"],
                     pw_hash, clean["gender"], clean["domicile"], clean["ethnicity"], clean["marga"], clean["religion"],
                     clean["marital_status"], clean["education"], clean["height_cm"], clean["intro"], clean["pref_age_min"],
                     clean["pref_age_max"], clean["pref_religion"], clean["pref_domicile"], clean["pref_notes"],
                     clean["publish_consent"], clean["source"], clean["perlu_review"], now)).lastrowid
                for slot, (name, enc) in stored.items():
                    conn.execute("INSERT INTO member_files(member_id,kind,stored_path,created_at,encrypted) VALUES(?,?,?,?,?)",
                                 (mid, slot, name, now, enc))
                sid = conn.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at) VALUES(?,?,'pending',?)",
                                   (mid, prog["id"], now)).lastrowid
                if prog["price_idr"]:  # NULL = unconfigured and 0 = free: neither gets an invoice
                    conn.execute("INSERT INTO payments(invoice_code,subscription_id,amount_idr,status,created_at) "
                                 "VALUES(?,?,?,'pending',?)",
                                 (db.next_invoice_code(conn, date.today()), sid, prog["price_idr"], now))
                db.audit(conn, None, "member_register", f"member={code} review={clean['perlu_review']}")
        except sqlite3.IntegrityError as ex:
            _cleanup(written)
            which = "phone" if "phone" in str(ex) else "email"
            return err({which: "Nomor HP sudah terdaftar." if which == "phone" else "Email sudah terdaftar."}, 409)
        except Exception as ex:
            _cleanup(written)
            log.error("register failed: %s", type(ex).__name__)  # class only: messages can echo values
            return JSONResponse({"ok": False, "error": "Terjadi kesalahan di server. Coba lagi."}, status_code=500)
        auth.drop_member_session(conn, s["token_hash"])
        tok, _, ttl = auth.create_member_session(conn, secret_key, mid)
        resp = JSONResponse({"ok": True, "redirect": "/akun"})
        set_cookie(resp, tok, ttl)
        return resp

    def _cleanup(names):
        for n in names:
            try:
                os.unlink(os.path.join(priv_dir, n))
            except OSError:
                pass

    @app.get("/akun")
    async def akun(request: Request):
        s = current(request)
        if not (s and s["member_id"]):
            return RedirectResponse("/akun/masuk", 303)
        m = conn.execute("SELECT code,full_name,status,perlu_review FROM members WHERE id=?", (s["member_id"],)).fetchone()
        sub = conn.execute("SELECT p.name,p.price_idr,s.status FROM subscriptions s JOIN programs p ON p.id=s.program_id "
                           "WHERE s.member_id=? ORDER BY s.id DESC LIMIT 1", (s["member_id"],)).fetchone()
        return render("public/akun.html", sess=s, m=m, sub=sub, price=db.price_label)

    @app.get("/akun/masuk")
    async def masuk_form(request: Request):
        s = current(request)
        if s and s["member_id"]:
            return RedirectResponse("/akun", 303)
        return with_session(request, "public/masuk.html")

    @app.post("/akun/masuk")
    async def masuk(request: Request):
        form = await request.form()
        s = current(request)
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return HTMLResponse("Forbidden", status_code=403)
        ident = str(form.get("ident", "")).strip().casefold()[:254]
        pw = str(form.get("password", ""))
        ip, key = client_ip(request), "m:" + ident[:60]
        if auth.is_locked(conn, key, ip):
            return render("public/masuk.html", status=429, sess=s, error="Terlalu banyak percobaan. Coba lagi nanti.")
        row = (conn.execute("SELECT id,password_hash FROM members WHERE email_key=?", (ident,)).fetchone() if "@" in ident
               else conn.execute("SELECT id,password_hash FROM members WHERE phone=?", (normalize_phone(ident),)).fetchone())
        too_long = len(pw) > 256
        good = auth.verify_password(row["password_hash"] if row and row["password_hash"] and not too_long
                                    else auth.DUMMY_HASH, pw) and not too_long
        if not (row and good):
            auth.record_fail(conn, key, ip)
            db.audit(conn, None, "member_login_fail")
            return render("public/masuk.html", status=401, sess=s, error="Email/HP atau password salah.")
        auth.clear_fails(conn, key)
        auth.drop_member_session(conn, s["token_hash"])
        tok, _, ttl = auth.create_member_session(conn, secret_key, row["id"])
        db.audit(conn, None, "member_login_ok")
        resp = RedirectResponse("/akun", 303)
        set_cookie(resp, tok, ttl)
        return resp

    @app.post("/akun/keluar")
    async def keluar(request: Request):
        form = await request.form()
        s = current(request)
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return HTMLResponse("Forbidden", status_code=403)
        auth.drop_member_session(conn, s["token_hash"])
        resp = RedirectResponse("/akun/masuk", 303)
        resp.delete_cookie(auth.MEMBER_COOKIE, path="/")
        return resp

    return fernet
