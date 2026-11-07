"""Stage 4 staff: finance payment queue (approve/reject proof) + Owner program config. No PII in audit."""
import json
import threading
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.responses import Response

import auth
import db
import portal
import register

FINANCE = {"finance", "owner"}
OWNER = {"owner"}
VERIFY = {"verifier", "owner"}
CHECKS = (("name", "Nama sesuai"), ("age", "Usia sesuai (18+)"), ("face", "Wajah cocok dengan foto"), ("censor", "NIK/alamat/tanda tangan tersensor"))
SLOT_OK = {"ktp_redacted", "photo_face", "photo_body"}
BATCH_MAX, MAX_RETENTION = 20, 3650


def _now():   # seam for boundary tests
    return datetime.now(timezone.utc).replace(microsecond=0)
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

    # ---- KTP/photo verification queue (verifier + owner; finance has no access) ----
    def vqueue(s, status=200, error=None):
        today = _now().date()
        rows = [dict(r, age=db.age_on(datetime.strptime(r["birth_date"], "%Y-%m-%d").date(), today)) for r in conn.execute(
            "SELECT id,code,full_name,birth_date,status,domicile FROM members m WHERE status='pending' AND EXISTS "
            "(SELECT 1 FROM member_files f WHERE f.member_id=m.id AND f.kind='ktp_redacted') ORDER BY id").fetchall()]
        return page("verif_queue.html", s, status, rows=rows, error=error)

    def vdetail(s, mid, status=200, error=None):
        m = conn.execute("SELECT id,code,full_name,birth_date,status,domicile FROM members WHERE id=?", (mid,)).fetchone()
        if not m:
            return PlainTextResponse("Not found", status_code=404)
        age = db.age_on(datetime.strptime(m["birth_date"], "%Y-%m-%d").date(), _now().date())
        return page("verif_detail.html", s, status, m=m, age=age, checks=CHECKS)

    @app.get("/verifikasi")
    async def vq(request: Request):
        s, bad = gate(request, VERIFY)
        return bad or vqueue(s)

    @app.get("/verifikasi/{mid}")
    async def vd(request: Request, mid: int):
        s, bad = gate(request, VERIFY)
        return bad or vdetail(s, mid)

    @app.get("/verifikasi/{mid}/berkas/{slot}")
    async def vfile(request: Request, mid: int, slot: str):
        s, bad = gate(request, VERIFY)
        if bad:
            return bad
        r = conn.execute("SELECT id,stored_path,encrypted FROM member_files WHERE member_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                         (mid, slot)).fetchone() if slot in SLOT_OK else None
        data = register.read_private(priv_dir, r["stored_path"], r["encrypted"], fernet) if r and (fernet or not r["encrypted"]) else None
        if data is None:
            return PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
        if slot == "ktp_redacted":   # IDs only: no name/NIK/path in audit
            db.audit(conn, s["staff_id"], "ktp_open", f"member={mid} file={r['id']}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def set_ver(mid, vstatus, sid, note, checklist, approved_at):   # inside db.tx
        for kind in ("ktp", "foto"):
            v = conn.execute("SELECT id FROM verifications WHERE member_id=? AND kind=? ORDER BY id DESC LIMIT 1", (mid, kind)).fetchone()
            if v:
                conn.execute("UPDATE verifications SET status=?,reviewer_id=?,note=?,checklist=?,approved_at=? WHERE id=?",
                             (vstatus, sid, note, checklist, approved_at, v["id"]))
            else:
                conn.execute("INSERT INTO verifications(member_id,kind,status,reviewer_id,note,checklist,approved_at,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (mid, kind, vstatus, sid, note, checklist, approved_at, db.now_iso()))

    @app.post("/verifikasi/{mid}/setuju")
    async def v_approve(request: Request, mid: int):
        s, form, bad = await gate_post(request, VERIFY)
        if bad:
            return bad
        checks = {k: form.get(k) == "1" for k, _ in CHECKS}
        if not all(checks.values()):
            return vdetail(s, mid, 400, "Semua checklist wajib dicentang sebelum menyetujui.")
        err = None
        with lock, db.tx(conn):
            m = conn.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()
            if not m:
                return PlainTextResponse("Not found", status_code=404)
            if m["status"] != "pending" or not conn.execute(
                    "SELECT 1 FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (mid,)).fetchone():
                err = "Member tidak dalam antrean verifikasi."
            else:   # approved_at = review time, never upload time; payments/subscriptions untouched
                set_ver(mid, "approved", s["staff_id"], None, json.dumps(checks), _now().strftime("%Y-%m-%dT%H:%M:%SZ"))
                conn.execute("UPDATE members SET status='verified' WHERE id=?", (mid,))
                db.audit(conn, s["staff_id"], "verification_approve", f"member={mid}")
        return vdetail(s, mid, 409, err) if err else RedirectResponse("/verifikasi", 303)

    @app.post("/verifikasi/{mid}/tolak")
    async def v_reject(request: Request, mid: int):
        s, form, bad = await gate_post(request, VERIFY)
        if bad:
            return bad
        reason = str(form.get("reason") or "").strip()
        if not 1 <= len(reason) <= 300:
            return vdetail(s, mid, 400, "Alasan penolakan wajib (1-300 karakter).")
        err = None
        with lock, db.tx(conn):
            m = conn.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()
            if not m:
                return PlainTextResponse("Not found", status_code=404)
            if m["status"] != "pending":
                err = "Member tidak dalam antrean verifikasi."
            else:
                set_ver(mid, "rejected", s["staff_id"], reason, None, None)
                conn.execute("UPDATE members SET status='rejected' WHERE id=?", (mid,))
                db.audit(conn, s["staff_id"], "verification_reject", f"member={mid}")   # reason stays out of audit
        return vdetail(s, mid, 409, err) if err else RedirectResponse("/verifikasi", 303)

    # ---- Owner-only KTP retention: GET only reads; deletion only by explicit confirmed POST, max 20 per click ----
    def retention_days():
        try:
            v = parse_opt_int(conn.execute("SELECT value FROM app_settings WHERE key='ktp_retention_days'").fetchone()[0], 1, MAX_RETENTION)
        except Exception:
            v = None
        return v or db.KTP_RETENTION_DAYS_DEFAULT

    def due_rows(days, limit=None):
        cutoff = (_now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")   # day N exactly = due
        q = ("SELECT v.member_id,f.id file_id,f.stored_path FROM verifications v JOIN member_files f ON f.member_id=v.member_id "
             "AND f.kind='ktp_redacted' WHERE v.kind='ktp' AND v.status='approved' AND v.approved_at IS NOT NULL "
             "AND v.approved_at<=? ORDER BY v.approved_at,f.id")
        return conn.execute(q + (" LIMIT ?" if limit else ""), (cutoff, limit) if limit else (cutoff,)).fetchall()

    def retention(s, status=200, error=None, msg=None):
        days = retention_days()
        due = due_rows(days)
        return page("retention.html", s, status, days=days, count=len(due), ids=due[:BATCH_MAX], batch=BATCH_MAX, error=error, msg=msg,
                    pending=conn.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0])

    @app.get("/owner/ktp-retensi")
    async def retention_get(request: Request):
        s, bad = gate(request, OWNER)
        return bad or retention(s)

    @app.post("/owner/ktp-retensi/hari")
    async def retention_set(request: Request):
        s, form, bad = await gate_post(request, OWNER)
        if bad:
            return bad
        try:
            days = parse_opt_int(form.get("days"), 1, MAX_RETENTION)
            if days is None:
                raise ValueError
        except ValueError:
            return retention(s, 400, f"Retensi harus bilangan bulat 1-{MAX_RETENTION} hari.")
        with lock, db.tx(conn):
            conn.execute("INSERT INTO app_settings(key,value) VALUES('ktp_retention_days',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))
            db.audit(conn, s["staff_id"], "ktp_retention_config", f"days={days}")
        return RedirectResponse("/owner/ktp-retensi", 303)

    @app.post("/owner/ktp-retensi/hapus")
    async def retention_run(request: Request):
        s, form, bad = await gate_post(request, OWNER)
        if bad:
            return bad
        if form.get("confirm") != "HAPUS":
            return retention(s, 400, "Konfirmasi wajib: ketik HAPUS.")
        with lock:
            with db.tx(conn):   # DB first: drop reference + durable cleanup intent, atomically
                rows = due_rows(retention_days(), BATCH_MAX)
                for r in rows:
                    conn.execute("DELETE FROM member_files WHERE id=?", (r["file_id"],))
                    conn.execute("INSERT OR IGNORE INTO pending_file_cleanup(member_id,stored_path,reason,created_at) "
                                 "VALUES(?,?,'retention',?)", (r["member_id"], r["stored_path"], db.now_iso()))
                    db.audit(conn, s["staff_id"], "ktp_retention_delete", f"member={r['member_id']} file={r['file_id']}")
            for mid in {r["member_id"] for r in rows}:   # post-commit; failures stay queued
                portal.reconcile_pending(conn, priv_dir, mid, BATCH_MAX)
        return retention(s, msg=f"{len(rows)} berkas KTP diproses.")
