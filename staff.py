"""Stage 4 staff: finance payment queue (approve/reject proof) + Owner program config. No PII in audit."""
import threading
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.responses import Response

import auth
import db
import register

FINANCE = {"finance", "owner"}
OWNER = {"owner"}
MAX_PRICE, MAX_DAYS, MAX_QUOTA = 1_000_000_000, 3650, 1000


def parse_opt_int(raw, lo, hi):
    """'' -> None (unconfigured); else int in [lo, hi]; ValueError otherwise."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 12 or not lo <= int(raw) <= hi:
        raise ValueError(raw)
    return int(raw)


def mount(app, conn, jenv, secret_key, cookie_secure, fernet, priv_dir):
    lock = threading.Lock()   # one shared sqlite connection: serialise tx blocks even if handlers ever run in threads

    def gate(request, roles):
        """(sess, None) if staff holds a role in `roles`, else (None, response). Roles read from DB per request."""
        tok = request.cookies.get(auth.COOKIE)
        with lock:   # same shared connection as the tx blocks below
            s = auth.load_session(conn, secret_key, tok) if tok else None
        if not (s and s["staff_id"]):
            return None, RedirectResponse("/login", 303)
        if not (s["roles"] & roles):
            return None, PlainTextResponse("Forbidden", status_code=403)
        return s, None

    async def gate_post(request, roles):
        s, bad = gate(request, roles)
        if bad:
            return None, None, bad
        form = await request.form()
        if not auth.csrf_ok(s, form.get("csrf_token")):
            return None, None, PlainTextResponse("Forbidden", status_code=403)
        return s, form, None

    def page(name, s, status=200, **ctx):
        return HTMLResponse(jenv.get_template(name).render(
            sess=s, csrf=s["csrf"], http_warning=not cookie_secure, labels=db.ROLE_LABEL, **ctx), status_code=status)

    def queue(s, status=200, error=None):
        rows = conn.execute(
            "SELECT p.id,p.invoice_code,p.amount_idr,p.created_at,p.proof_file_id,m.code,pr.name,pr.duration_days "
            "FROM payments p JOIN subscriptions su ON su.id=p.subscription_id JOIN members m ON m.id=su.member_id "
            "JOIN programs pr ON pr.id=su.program_id WHERE p.status='pending' ORDER BY p.id").fetchall()
        return page("finance.html", s, status, rows=rows, error=error, label=db.price_label)

    @app.get("/keuangan")
    async def finance_queue(request: Request):
        s, bad = gate(request, FINANCE)
        return bad or queue(s)

    @app.get("/keuangan/{pid}/bukti")
    async def finance_proof(request: Request, pid: int):
        s, bad = gate(request, FINANCE)
        if bad:
            return bad
        r = conn.execute("SELECT f.stored_path,f.encrypted FROM payments p JOIN member_files f ON f.id=p.proof_file_id "
                         "WHERE p.id=? AND f.kind='payment_proof'", (pid,)).fetchone()
        data = register.read_private(priv_dir, r["stored_path"], r["encrypted"], fernet) if r and fernet else None
        if data is None:
            return PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
        db.audit(conn, s["staff_id"], "payment_proof_view", f"payment={pid}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/keuangan/{pid}/setuju")
    async def finance_approve(request: Request, pid: int):
        s, _, bad = await gate_post(request, FINANCE)
        if bad:
            return bad
        err = None
        with lock, db.tx(conn):   # BEGIN IMMEDIATE: second concurrent approve re-reads status and sees 'approved'
            r = conn.execute("SELECT p.status,p.proof_file_id,su.id sid,pr.duration_days FROM payments p "
                             "JOIN subscriptions su ON su.id=p.subscription_id JOIN programs pr ON pr.id=su.program_id "
                             "WHERE p.id=?", (pid,)).fetchone()
            if not r:
                return PlainTextResponse("Not found", status_code=404)
            if r["status"] != "pending":
                err = "Pembayaran sudah diproses."
            elif not r["proof_file_id"]:
                err = "Belum ada bukti transfer."
            elif not r["duration_days"] or r["duration_days"] <= 0:
                err = "Durasi program belum dikonfigurasi Owner; persetujuan diblokir."
            else:
                now = datetime.now(timezone.utc).replace(microsecond=0)
                fmt = "%Y-%m-%dT%H:%M:%SZ"
                conn.execute("UPDATE payments SET status='approved',reject_reason=NULL WHERE id=?", (pid,))
                conn.execute("UPDATE subscriptions SET status='active',starts_at=?,ends_at=? WHERE id=?",
                             (now.strftime(fmt), (now + timedelta(days=r["duration_days"])).strftime(fmt), r["sid"]))
                db.audit(conn, s["staff_id"], "payment_approve", f"payment={pid}")
        return queue(s, 409, err) if err else RedirectResponse("/keuangan", 303)

    @app.post("/keuangan/{pid}/tolak")
    async def finance_reject(request: Request, pid: int):
        s, form, bad = await gate_post(request, FINANCE)
        if bad:
            return bad
        reason = str(form.get("reason") or "").strip()
        if not 1 <= len(reason) <= 300:
            return queue(s, 400, "Alasan penolakan wajib (1-300 karakter).")
        err = None
        with lock, db.tx(conn):
            r = conn.execute("SELECT status,proof_file_id FROM payments WHERE id=?", (pid,)).fetchone()
            if not r:
                return PlainTextResponse("Not found", status_code=404)
            if r["status"] != "pending" or not r["proof_file_id"]:
                err = "Pembayaran tidak dalam status menunggu dengan bukti."
            else:
                conn.execute("UPDATE payments SET status='rejected',reject_reason=? WHERE id=?", (reason, pid))
                db.audit(conn, s["staff_id"], "payment_reject", f"payment={pid}")   # reason stays out of audit
        return queue(s, 409, err) if err else RedirectResponse("/keuangan", 303)

    def programs(s, status=200, error=None):
        rows = conn.execute("SELECT id,name,price_idr,duration_days,intro_quota FROM programs ORDER BY sort_order,id").fetchall()
        return page("programs.html", s, status, rows=rows, error=error)

    @app.get("/owner/program")
    async def programs_get(request: Request):
        s, bad = gate(request, OWNER)
        return bad or programs(s)

    @app.post("/owner/program/{prog_id}")
    async def programs_set(request: Request, prog_id: int):
        s, form, bad = await gate_post(request, OWNER)
        if bad:
            return bad
        try:
            price = parse_opt_int(form.get("price_idr"), 0, MAX_PRICE)
            days = parse_opt_int(form.get("duration_days"), 1, MAX_DAYS)
            quota = parse_opt_int(form.get("intro_quota"), 0, MAX_QUOTA)
        except ValueError:
            return programs(s, 400, "Nilai tidak valid (harga >=0, durasi >=1 hari, kuota >=0; kosong = belum diatur).")
        with lock, db.tx(conn):
            if not conn.execute("UPDATE programs SET price_idr=?,duration_days=?,intro_quota=? WHERE id=?",
                                (price, days, quota, prog_id)).rowcount:
                return PlainTextResponse("Not found", status_code=404)
            db.audit(conn, s["staff_id"], "program_config", f"program={prog_id} price={price} days={days} quota={quota}")
        return RedirectResponse("/owner/program", 303)
