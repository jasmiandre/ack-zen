"""ACK Stage 0: staff auth foundation (FastAPI + Jinja2 + SQLite). Dummy data only."""
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from cryptography.fernet import Fernet
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

import auth
import landing
import db
import portal
import register
from register import BodyLimit
import seed
import matching
import pipeline
import staff
import ops
import event_pairs
import events

HERE = os.path.dirname(os.path.abspath(__file__))
USER_RE = re.compile(r"[a-z0-9._-]{3,32}")
AREAS = tuple(r for r in db.ROLES if r != "owner")


PILOT_BLOCKED = re.compile(r"/(daftar/submit|event/tamu/submit|event/[^/]+/daftar|akun/(event/bukti|bayar/unggah|verifikasi/unggah)"
                           r"|keuangan/(event/)?\d+/setuju)")

def create_app(db_path=None, secret_key=None, cookie_secure=None, fernet_key=None, bank_info=None, wa_admin=None, pilot_open=None):
    env = seed.read_env(seed.ENV_PATH)
    db_path = db_path or seed.DB_PATH
    secret_key = secret_key or env.get("SECRET_KEY") or ""
    if len(secret_key) < 32:
        raise RuntimeError("SECRET_KEY missing/short: run ./run.sh (seed.py generates .env once)")
    if fernet_key is None:  # "" = explicitly unconfigured (registration answers 503, never stores plaintext KTP)
        fernet_key = env.get("FERNET_KEY") or ""
    if cookie_secure is None:
        cookie_secure = env.get("COOKIE_SECURE", "false").lower() == "true"

    if pilot_open is None:  # strict: only the exact string "true" opens intake; anything else stays closed
        pilot_open = (os.environ.get("PILOT_INTAKE_OPEN") or env.get("PILOT_INTAKE_OPEN") or "").strip() == "true"
    app_state_open = bool(pilot_open)

    # ponytail: one shared sqlite connection, handlers are async (no await between DB calls) so writers
    # never interleave. Move to a connection-per-request pool if handlers become sync/threaded.
    conn = db.connect(db_path)
    db.init_db(conn)
    jenv = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                       autoescape=select_autoescape(["html"]))
    @asynccontextmanager
    async def lifespan(_app):
        yield
        conn.close()
        _app.state.conn_closed = True

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)  # no API docs; static = explicit css/js/fonts mounts only (never data/.env)

    for sub in ("css", "js", "fonts", "img"):
        app.mount(f"/static/{sub}", StaticFiles(directory=os.path.join(HERE, "static", sub)), name=f"static-{sub}")

    priv = os.path.join(os.path.dirname(os.path.abspath(db_path)), "private")
    fernet = register.mount(app, conn, jenv, secret_key, cookie_secure, fernet_key, priv)
    portal.mount(app, conn, jenv, secret_key, cookie_secure, fernet, priv,
                 env.get("BANK_INFO", "") if bank_info is None else bank_info,
                 env.get("WA_ADMIN", "") if wa_admin is None else wa_admin)

    staff.mount(app, conn, jenv, secret_key, cookie_secure, Fernet(fernet_key.encode()) if fernet_key else None, priv)

    ops.mount(app, conn, jenv, secret_key, cookie_secure, Fernet(fernet_key.encode()) if fernet_key else None, priv)
    pipeline.mount(app, conn, jenv, secret_key, cookie_secure)
    matching.mount(app, conn, jenv, secret_key, cookie_secure)
    events.mount(app, conn, jenv, secret_key, cookie_secure, fernet, priv)
    event_pairs.mount(app, conn, jenv, secret_key, cookie_secure)

    def page(request, name, status=200, sess=None, **ctx):
        html = jenv.get_template(name).render(
            sess=sess, csrf=(sess or {}).get("csrf", ""), http_warning=not cookie_secure,
            labels=db.ROLE_LABEL, **ctx)
        return HTMLResponse(html, status_code=status)

    def set_cookie(resp, tok, ttl):
        resp.set_cookie(auth.COOKIE, tok, max_age=ttl, httponly=True, samesite="lax",
                        secure=cookie_secure, path="/")

    def current(request):
        tok = request.cookies.get(auth.COOKIE)
        return auth.load_session(conn, secret_key, tok) if tok else None

    def staff_or_redirect(request):
        s = current(request)
        return (s, None) if s and s["staff_id"] else (None, RedirectResponse("/login", 303))

    def forbidden():
        return PlainTextResponse("Forbidden", status_code=403)

    @app.middleware("http")
    async def pilot_gate(request: Request, call_next):  # closed pilot: refuse before any handler touches files/DB
        if not app_state_open and request.method == "POST" and PILOT_BLOCKED.fullmatch(request.url.path):
            return PlainTextResponse("Pendaftaran & pembayaran pilot belum dibuka (PILOT_INTAKE_OPEN=true diperlukan).",
                                     status_code=503, headers={"Retry-After": "86400"})
        return await call_next(request)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.url.path == "/":  # public landing: pinned CDN allow-list; staff pages keep the tight CSP
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["Content-Security-Policy"] = landing.PUBLIC_CSP
        elif request.url.path in ("/daftar", "/akun/masuk") or (request.url.path.startswith("/event/") and request.url.path.endswith("/tamu")) or request.url.path.startswith("/akun") and not request.url.path.startswith("/akun/foto/"):  # self-hosted JS/CSS only; blob: preview of redacted KTP
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                "font-src 'self'; img-src 'self' blob: data:; connect-src 'self'; form-action 'self'; "
                "base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
        elif request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "public, max-age=3600"
            resp.headers["Content-Security-Policy"] = "default-src 'none'"
        else:
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        return resp

    FILE_VIEWERS = {"verifier", "owner"}  # least privilege: only identity verification needs member photos / KTP

    @app.get("/berkas/{file_id}")
    async def member_file(request: Request, file_id: int):
        s, redir = staff_or_redirect(request)
        if redir:
            return redir
        if not (s["roles"] & FILE_VIEWERS):
            return forbidden()
        r = conn.execute("SELECT member_id,kind,stored_path,encrypted FROM member_files WHERE id=?", (file_id,)).fetchone()
        data = register.read_private(os.path.join(os.path.dirname(os.path.abspath(db_path)), "private"),
                                     r["stored_path"], r["encrypted"], Fernet(fernet_key.encode())) if r and fernet_key else None
        if data is None:
            return PlainTextResponse("Not found", status_code=404)
        db.audit(conn, s["staff_id"], "file_view", f"file={file_id} kind={r['kind']}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        ok = conn.execute("SELECT 1").fetchone()[0] == 1
        return JSONResponse({"status": "ok", "stage": 0, "db": ok})

    @app.get("/")
    async def root(request: Request):
        c = landing.load_content()
        html = jenv.get_template("public/landing.html").render(c=c, cd=landing.countdown(c["event"]["date_iso"]))
        return HTMLResponse(html)

    @app.get("/login")
    async def login_form(request: Request):
        s = current(request)
        if s and s["staff_id"]:
            return RedirectResponse("/dashboard", 303)
        if not s:
            tok, csrf, ttl = auth.create_session(conn, secret_key)
            s = {"csrf": csrf}
            resp = page(request, "login.html", sess=s)
            set_cookie(resp, tok, ttl)
            return resp
        return page(request, "login.html", sess=s)

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        s = current(request)
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return forbidden()
        user = str(form.get("username", "")).strip()[:64]
        pw = str(form.get("password", ""))
        too_long = len(pw) > 256  # reject, never truncate: a 256-char prefix must not authenticate
        ip = request.client.host if request.client else "?"
        if auth.is_locked(conn, user, ip):
            db.audit(conn, None, "login_locked")
            return page(request, "login.html", 429, sess=s, error="Terlalu banyak percobaan. Coba lagi nanti.")
        row = conn.execute("SELECT id,password_hash,active FROM users_staff WHERE username=?", (user,)).fetchone()
        good = auth.verify_password(row["password_hash"] if row and not too_long else auth.DUMMY_HASH, pw) and not too_long
        if not (row and good and row["active"]):
            auth.record_fail(conn, user, ip)
            db.audit(conn, None, "login_fail", "known" if row else "unknown")  # never log the typed values
            return page(request, "login.html", 401, sess=s, error="Username atau password salah.")
        auth.clear_fails(conn, user)
        auth.drop_session(conn, s["token_hash"])
        tok, _, ttl = auth.create_session(conn, secret_key, row["id"])  # rotation: new token + new csrf
        db.audit(conn, row["id"], "login_ok")
        resp = RedirectResponse("/dashboard", 303)
        set_cookie(resp, tok, ttl)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        s = current(request)
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return forbidden()
        auth.drop_session(conn, s["token_hash"])
        db.audit(conn, s["staff_id"], "logout")
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.get("/dashboard")
    async def dashboard(request: Request):
        s, redir = staff_or_redirect(request)
        if redir:
            return redir
        return page(request, "dashboard.html", sess=s, areas=AREAS if "owner" in s["roles"]
                    else [r for r in AREAS if r in s["roles"]],
                    stats=pipeline.stats(conn, s["roles"]))

    @app.get("/access/{role}")
    async def access(request: Request, role: str):
        s, redir = staff_or_redirect(request)
        if redir:
            return redir
        if role not in AREAS:
            return PlainTextResponse("Not found", status_code=404)
        if role not in s["roles"] and "owner" not in s["roles"]:
            return forbidden()
        return page(request, "access.html", sess=s, role=role)

    def owner_only(request):
        s, redir = staff_or_redirect(request)
        if redir:
            return None, redir
        return (s, None) if "owner" in s["roles"] else (None, forbidden())

    def staff_list():
        rows = conn.execute(
            "SELECT u.id,u.username,u.display_name,u.active,COALESCE(group_concat(r.role,', '),'') AS roles "
            "FROM users_staff u LEFT JOIN staff_roles r ON r.staff_id=u.id GROUP BY u.id ORDER BY u.id").fetchall()
        return rows

    @app.get("/owner/staff")
    async def staff_get(request: Request):
        s, bad = owner_only(request)
        return bad or page(request, "staff.html", sess=s, staff=staff_list(), all_roles=db.ROLES)

    @app.post("/owner/staff")
    async def staff_create(request: Request):
        s, bad = owner_only(request)
        if bad:
            return bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return forbidden()
        username = str(form.get("username", "")).strip()
        display = str(form.get("display_name", "")).strip()
        pw = str(form.get("password", ""))
        roles = [r for r in form.getlist("roles") if r in db.ROLES]
        err = None
        if not USER_RE.fullmatch(username):
            err = "Username: 3-32 karakter a-z 0-9 . _ -"
        elif not 1 <= len(display) <= 80:
            err = "Nama tampil 1-80 karakter"
        elif not 12 <= len(pw) <= 256:
            err = "Password minimal 12 karakter"
        elif not roles or len(roles) != len(form.getlist("roles")):
            err = "Pilih role yang valid"
        elif conn.execute("SELECT 1 FROM users_staff WHERE username=?", (username,)).fetchone():
            err = "Username sudah dipakai"
        if err:
            return page(request, "staff.html", 400, sess=s, staff=staff_list(), all_roles=db.ROLES, error=err)
        h = auth.hash_password(pw)
        with db.tx(conn):
            cur = conn.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,?)",
                               (username, display, h, db.now_iso()))
            conn.executemany("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", [(cur.lastrowid, r) for r in set(roles)])
            db.audit(conn, s["staff_id"], "staff_create", f"{username}:{','.join(sorted(set(roles)))}")
        return RedirectResponse("/owner/staff", 303)

    @app.post("/owner/staff/{staff_id}/disable")
    async def staff_disable(request: Request, staff_id: int):
        s, bad = owner_only(request)
        if bad:
            return bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return forbidden()
        if staff_id == s["staff_id"]:
            return page(request, "staff.html", 400, sess=s, staff=staff_list(), all_roles=db.ROLES,
                        error="Tidak boleh menonaktifkan akun sendiri")
        with db.tx(conn):
            conn.execute("UPDATE users_staff SET active=0 WHERE id=?", (staff_id,))
            conn.execute("DELETE FROM sessions WHERE staff_id=?", (staff_id,))  # server-side revocation
            db.audit(conn, s["staff_id"], "staff_disable", str(staff_id))
        return RedirectResponse("/owner/staff", 303)

    @app.get("/owner/audit")
    async def audit_get(request: Request):
        s, bad = owner_only(request)
        if bad:
            return bad
        q = request.query_params
        act = (q.get("action") or "").strip()[:60]
        pg = int(q["page"]) if (q.get("page") or "").isascii() and (q.get("page") or "").isdigit() and len(q["page"]) <= 6 and int(q["page"]) >= 1 else 1
        where, args = ("WHERE a.action=? ", (act,)) if act else ("", ())
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log a {where}", args).fetchone()[0]
        rows = conn.execute("SELECT a.ts,a.action,a.detail,u.username FROM audit_log a "
                            f"LEFT JOIN users_staff u ON u.id=a.staff_id {where}ORDER BY a.id DESC LIMIT 50 OFFSET ?",
                            args + ((pg - 1) * 50,)).fetchall()
        actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY 1")]
        return page(request, "audit.html", sess=s, rows=rows, act=act, pg=pg, pages=max(1, -(-total // 50)), actions=actions)

    # raw ASGI wrapper: cap is enforced on received bytes before multipart parsing
    app.add_middleware(BodyLimit, path="/daftar/submit", limit=register.BODY_LIMIT)
    for up in ("/akun/bayar/unggah", "/akun/verifikasi/unggah", "/akun/event/bukti", "/event/tamu/submit"):
        app.add_middleware(BodyLimit, path=up, limit=register.BODY_LIMIT)
    return app
