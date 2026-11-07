"""ACK Stage 3: member portal (/akun). Every query is scoped by the session's member_id (no id from the client
is trusted without an ownership join). Audit rows carry the member code only, never form values or paths."""
import os
import secrets
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import auth
import db
import matching
import register

STEPS = ("Pendaftaran", "Pembayaran", "Verifikasi", "Aktif", "Perkenalan")
PAY_LABEL = {"unpaid": "Menunggu Bayar", "check": "Menunggu Dicek", "approved": "Lunas", "rejected": "Ditolak"}
VER_LABEL = {"pending": "Menunggu dicek tim", "approved": "Terverifikasi", "rejected": "Ditolak"}
VER_KIND = {"ktp": ("KTP (sensor)", ("ktp_redacted",)), "foto": ("Foto", ("photo_body", "photo_face"))}
CORE = ("ethnicity", "religion", "marital_status")      # change -> perlu_review
EDITABLE = CORE + ("domicile", "marga", "occupation", "education", "height_cm", "intro", "pref_age_min", "pref_age_max",
                   "pref_religion", "pref_domicile", "pref_notes", "publish_consent")


def timeline(paid, verified, active, introduced, invoiced=True):
    """Each stage shows its own fact (a later stage can be done while an earlier one is not: no gating is invented).
    First unfinished stage is 'current', other unfinished ones 'todo'; unpaid without any invoice is 'na'."""
    out, cur = [(STEPS[0], "done")], False
    for name, ok in zip(STEPS[1:], (paid, verified, active, introduced)):
        if ok:
            out.append((name, "done"))
        elif name == STEPS[1] and not invoiced:
            out.append((name, "na"))
        else:
            out.append((name, "todo" if cur else "current"))
            cur = True
    return out


def reconcile_pending(conn, priv_dir, mid=None, limit=20):
    """Bounded, explicit retry of file cleanups (one member, or all if mid is None). Never raises; returns #removed.
    Unreferenced files are unlinked (already-missing counts as done); failures stay queued with attempts/last_error."""
    done = 0
    try:
        rows = conn.execute("SELECT id,stored_path FROM pending_file_cleanup WHERE (? IS NULL OR member_id=?) "
                            "ORDER BY id LIMIT ?", (mid, mid, limit)).fetchall()
        for rid, path in rows:
            try:
                if register.FILE_RE.fullmatch(path) and not conn.execute(
                        "SELECT 1 FROM member_files WHERE stored_path=?", (path,)).fetchone():
                    try:
                        os.unlink(os.path.join(priv_dir, path))
                    except FileNotFoundError:
                        pass
                conn.execute("DELETE FROM pending_file_cleanup WHERE id=?", (rid,))
                done += 1
            except Exception as ex:
                conn.execute("UPDATE pending_file_cleanup SET attempts=attempts+1,last_error=? WHERE id=?",
                             (type(ex).__name__, rid))
    except Exception:
        pass   # table/DB trouble: rows stay for the next explicit call
    return done


def mount(app, conn, jenv, secret_key, cookie_secure, fernet, priv_dir, bank_info="", wa_admin=""):
    bank_lines = [x.strip() for x in (bank_info or "").split("|") if x.strip()]
    wa = "".join(ch for ch in (wa_admin or "") if ch.isdigit())

    def current(request):
        tok = request.cookies.get(auth.MEMBER_COOKIE)
        s = auth.load_member_session(conn, secret_key, tok) if tok else None
        return s if s and s["member_id"] else None

    def render(name, s, status=200, **ctx):
        return HTMLResponse(jenv.get_template(name).render(csrf=s["csrf"], http_warning=not cookie_secure, **ctx),
                            status_code=status)

    def me(s):
        return conn.execute("SELECT * FROM members WHERE id=?", (s["member_id"],)).fetchone()

    def audit(m, action, extra=""):
        db.audit(conn, None, action, f"member={m['code']}" + (f" {extra}" if extra else ""))

    def latest_payment(mid):
        return conn.execute(
            "SELECT p.* FROM payments p JOIN subscriptions s ON s.id=p.subscription_id WHERE s.member_id=? "
            "ORDER BY p.id DESC LIMIT 1", (mid,)).fetchone()

    def ver_row(mid, kind):
        return conn.execute("SELECT * FROM verifications WHERE member_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                            (mid, kind)).fetchone()

    # File lifecycle: a private file is only ever unlinked via a durable pending_file_cleanup row (member scoped).
    # New files get their row BEFORE being written and lose it in the same DB tx that references them; replaced
    # files gain a row in the same tx that drops their member_files row. So rollback/crash/unlink failure can never
    # lose a referenced file nor leave an untracked orphan; reconcile() retries what is left.
    def stage_file(mid, data, ext):
        name = secrets.token_hex(16) + ext
        conn.execute("INSERT INTO pending_file_cleanup(member_id,stored_path,reason,created_at) VALUES(?,?,'new',?)",
                     (mid, name, db.now_iso()))
        register._write_private(priv_dir, data, ext, name)
        return name

    def rm_file(fid):   # call inside db.tx; the unlink happens later in reconcile()
        r = conn.execute("SELECT member_id,stored_path FROM member_files WHERE id=?", (fid,)).fetchone()
        conn.execute("DELETE FROM member_files WHERE id=?", (fid,))
        if r:
            conn.execute("INSERT OR IGNORE INTO pending_file_cleanup(member_id,stored_path,reason,created_at) "
                         "VALUES(?,?,'replaced',?)", (r[0], r[1], db.now_iso()))

    def commit_new(names):   # inside db.tx: these files are now referenced
        for n in names:
            conn.execute("DELETE FROM pending_file_cleanup WHERE stored_path=?", (n,))

    def reconcile(mid, limit=20):
        reconcile_pending(conn, priv_dir, mid, limit)

    def intro_cards(mid, platinum):
        rows = conn.execute("SELECT * FROM introductions WHERE (member_a=? OR member_b=?) AND status IN "
                            "('proposed','mutual','declined') ORDER BY id DESC", (mid, mid)).fetchall()
        today, cards = datetime.now(timezone.utc).date(), []
        for r in rows:
            a_side = r["member_a"] == mid
            mine, theirs = (r["resp_a"], r["resp_b"]) if a_side else (r["resp_b"], r["resp_a"])
            o = conn.execute("SELECT * FROM members WHERE id=?", (r["member_b"] if a_side else r["member_a"],)).fetchone()
            slots = conn.execute("SELECT * FROM dinner_slots WHERE introduction_id=? ORDER BY starts_at",
                                 (r["id"],)).fetchall() if r["status"] == "mutual" else []
            picked = next((x for x in slots if x["picked_by"]), None)
            fb = conn.execute("SELECT 1 FROM feedback WHERE introduction_id=? AND member_id=?", (r["id"], mid)).fetchone()
            cards.append(dict(
                id=r["id"], status=r["status"], mine=mine, theirs=theirs, reason=r["match_reason"],
                age=db.age_on(db.parse_birth_date(o["birth_date"]), today), domicile=o["domicile"], education=o["education"], occupation=o["occupation"],
                intro=o["intro"], mutual=r["status"] == "mutual",
                contact=dict(name=o["full_name"], phone=o["phone"]) if r["status"] == "mutual" else None,
                slots=slots if platinum or picked else [], pick=platinum and not picked, picked=picked,
                feedback_done=bool(fb)))
        return cards

    def dashboard(s, status=200, error=None):
        m, mid = me(s), s["member_id"]
        sub = conn.execute("SELECT p.name,p.price_idr,s.status FROM subscriptions s JOIN programs p ON p.id=s.program_id "
                           "WHERE s.member_id=? ORDER BY s.id DESC LIMIT 1", (mid,)).fetchone()
        pay = latest_payment(mid)
        pstate = None
        if pay:
            pstate = ("approved" if pay["status"] == "approved" else "rejected" if pay["status"] == "rejected"
                      else "check" if pay["proof_file_id"] else "unpaid")
        base = pay["amount_idr"] + (pay["unique_code"] or 0) if pay and pay["amount_idr"] is not None else None
        vers = {k: ver_row(mid, k) for k in VER_KIND}
        vstate = {k: (v["status"] if v else "pending") for k, v in vers.items()}
        platinum = bool(sub and "platinum" in sub["name"].lower())
        cards = intro_cards(mid, platinum)
        steps = timeline(pstate == "approved", m["status"] == "verified", bool(sub and sub["status"] == "active"),
                         any(c["mutual"] for c in cards), invoiced=bool(pay))
        return render("public/akun.html", s, status, m=m, sub=sub, steps=steps, pay=pay, pstate=pstate,
                      pay_label=PAY_LABEL, ver_label=VER_LABEL, total=base, rp=lambda n: "Rp " + f"{n:,}".replace(",", "."),
                      bank=bank_lines, wa=wa, price=db.price_label, vers=vers, vstate=vstate, vkind=VER_KIND,
                      cards=cards, deletion=conn.execute("SELECT 1 FROM deletion_requests WHERE member_id=? AND status='open'",
                                                         (mid,)).fetchone(), error=error,
                      overdue=bool(pay and pstate == "unpaid" and pay["due_at"] and pay["due_at"] < db.now_iso()))

    def profile_page(s, m, status=200, errors=None, vals=None):
        return render("public/profil.html", s, status, m=m, v=vals or m, errors=errors or {},
                      o=dict(ethnicity=register.ETHNICITY, religion=register.RELIGION, marital=register.MARITAL,
                             education=register.EDUCATION, publish=register.PUBLISH, pref_religion=register.PREF_RELIGION))

    async def gate(request):
        """-> (session, form, None) or (None, None, response). CSRF is checked before anything else."""
        s = current(request)
        if not s:
            return None, None, RedirectResponse("/akun/masuk", 303)
        try:
            form = await request.form(max_files=4, max_fields=40)
        except Exception:
            return None, None, HTMLResponse("Permintaan tidak valid", status_code=400)
        if not auth.csrf_ok(s, str(form.get("csrf_token") or "")):
            await form.close()
            return None, None, HTMLResponse("Forbidden", status_code=403)
        return s, form, None

    async def one_image(form, slots):
        """Exactly the allowed upload slots, each a valid image -> {slot: jpeg bytes}; raises ValueError(msg)."""
        if fernet is None:
            raise ValueError("Penyimpanan aman belum dikonfigurasi.")
        ups = {k: v for k, v in form.items() if not isinstance(v, str)}
        if not ups or set(ups) - set(slots):
            raise ValueError("Pilih file gambar yang sesuai.")
        out = {}
        for k, up in ups.items():
            out[k] = register.clean_image(await up.read(register.MAX_IMG + 1), up.filename, up.content_type)
        return out

    @app.get("/akun")
    async def akun(request: Request):
        s = current(request)
        return dashboard(s) if s else RedirectResponse("/akun/masuk", 303)

    @app.post("/akun/bayar/unggah")
    async def pay_upload(request: Request):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            m, pay = me(s), latest_payment(s["member_id"])
            if not pay or pay["status"] == "approved":
                return dashboard(s, 400, "Tidak ada tagihan yang perlu bukti transfer.")
            try:
                img = await one_image(form, ("proof",))
            except ValueError as ex:
                return dashboard(s, 400, str(ex))
            try:
                name = stage_file(s["member_id"], fernet.encrypt(img["proof"]), ".enc")
            except BaseException:
                reconcile(s["member_id"])
                raise
            try:
                with db.tx(conn):
                    commit_new([name])
                    if pay["proof_file_id"]:
                        rm_file(pay["proof_file_id"])
                    fid = conn.execute("INSERT INTO member_files(member_id,kind,stored_path,created_at,encrypted) "
                                       "VALUES(?,?,?,?,1)", (s["member_id"], "payment_proof", name, db.now_iso())).lastrowid
                    conn.execute("UPDATE payments SET proof_file_id=?,status='pending',reject_reason=NULL WHERE id=?",
                                 (fid, pay["id"]))
                    audit(m, "member_payment_proof")
            except BaseException:
                reconcile(s["member_id"])
                raise
            reconcile(s["member_id"])
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()

    @app.post("/akun/verifikasi/unggah")
    async def ver_upload(request: Request):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            m, mid, kind = me(s), s["member_id"], str(form.get("kind") or "")
            v = ver_row(mid, kind) if kind in VER_KIND else None
            if not v or v["status"] != "rejected":
                return dashboard(s, 400, "Unggah ulang hanya untuk berkas yang ditolak.")
            try:
                imgs = await one_image(form, VER_KIND[kind][1])
            except ValueError as ex:
                return dashboard(s, 400, str(ex))
            written = []
            try:
                for slot, d in imgs.items():
                    enc = slot == "ktp_redacted"
                    written.append((slot, stage_file(mid, fernet.encrypt(d) if enc else d, ".enc" if enc else ".jpg")))
            except BaseException:
                reconcile(mid)    # removes the files already written for earlier slots
                raise
            try:
                with db.tx(conn):
                    commit_new([n for _, n in written])
                    for slot, name in written:
                        for old in conn.execute("SELECT id FROM member_files WHERE member_id=? AND kind=?", (mid, slot)).fetchall():
                            rm_file(old[0])
                        conn.execute("INSERT INTO member_files(member_id,kind,stored_path,created_at,encrypted) VALUES(?,?,?,?,?)",
                                     (mid, slot, name, db.now_iso(), int(slot == "ktp_redacted")))
                    conn.execute("UPDATE verifications SET status='pending',note=NULL,reviewer_id=NULL WHERE id=?", (v["id"],))
                    conn.execute("UPDATE members SET status='pending' WHERE id=? AND status='rejected'", (mid,))
                    audit(m, "member_reupload", f"kind={kind}")
            except BaseException:
                reconcile(mid)
                raise
            reconcile(mid)
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()

    def party(mid, iid):
        return conn.execute("SELECT * FROM introductions WHERE id=? AND (member_a=? OR member_b=?)", (iid, mid, mid)).fetchone()

    @app.get("/akun/foto/{iid}")
    async def counterpart_photo(request: Request, iid: int):
        s = current(request)
        if not s:
            return RedirectResponse("/akun/masuk", 303)
        r = party(s["member_id"], iid)
        if not r or r["status"] not in ("proposed", "mutual"):
            return Response("Not found", status_code=404)
        other = r["member_b"] if r["member_a"] == s["member_id"] else r["member_a"]
        f = conn.execute("SELECT stored_path FROM member_files WHERE member_id=? AND kind='photo_face' ORDER BY id DESC LIMIT 1",
                         (other,)).fetchone()
        data = register.read_private(priv_dir, f[0], 0, fernet) if f else None
        if data is None:
            return Response("Not found", status_code=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/akun/perkenalan/{iid}/respon")
    async def respond(request: Request, iid: int):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            r = party(s["member_id"], iid)
            if not r:
                return Response("Not found", status_code=404)
            choice = str(form.get("choice") or "")
            col = "resp_a" if r["member_a"] == s["member_id"] else "resp_b"
            if choice not in ("tertarik", "tidak") or r["status"] != "proposed" or r[col]:
                return dashboard(s, 400, "Pilihan tidak dapat diubah atau tidak valid.")
            with db.tx(conn):
                matching.apply_response(conn, iid, col, choice)
                audit(me(s), "member_intro_response", f"intro={iid}")
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()

    @app.post("/akun/perkenalan/{iid}/slot")
    async def pick_slot(request: Request, iid: int):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            r = party(s["member_id"], iid)
            if not r:
                return Response("Not found", status_code=404)
            sub = conn.execute("SELECT p.name FROM subscriptions x JOIN programs p ON p.id=x.program_id WHERE x.member_id=? "
                               "ORDER BY x.id DESC LIMIT 1", (s["member_id"],)).fetchone()
            try:
                sid = int(form.get("slot_id"))
            except (TypeError, ValueError):
                sid = 0
            if not (sub and "platinum" in sub[0].lower()) or r["status"] != "mutual":
                return dashboard(s, 400, "Pilihan jadwal hanya untuk member Platinum setelah saling tertarik.")
            with db.tx(conn):
                n = conn.execute(
                    "UPDATE dinner_slots SET picked_by=? WHERE id=? AND introduction_id=? AND picked_by IS NULL AND NOT EXISTS "
                    "(SELECT 1 FROM dinner_slots WHERE introduction_id=? AND picked_by IS NOT NULL)",
                    (s["member_id"], sid, iid, iid)).rowcount
                if n:
                    audit(me(s), "member_slot_pick", f"intro={iid}")
            return RedirectResponse("/akun", 303) if n else dashboard(s, 400, "Jadwal tidak tersedia.")
        finally:
            await form.close()

    @app.post("/akun/perkenalan/{iid}/feedback")
    async def feedback(request: Request, iid: int):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            r = party(s["member_id"], iid)
            if not r:
                return Response("Not found", status_code=404)
            note = str(form.get("note") or "").strip()
            rating = str(form.get("rating") or "")
            if r["status"] != "mutual" or rating not in "12345" or not rating or len(note) > 1000:
                return dashboard(s, 400, "Rating 1-5 wajib; catatan maksimal 1000 karakter.")
            try:
                conn.execute("INSERT INTO feedback(introduction_id,member_id,rating,note,created_at) VALUES(?,?,?,?,?)",
                             (iid, s["member_id"], int(rating), note, db.now_iso()))
            except db.sqlite3.IntegrityError:
                return dashboard(s, 400, "Feedback sudah dikirim.")
            audit(me(s), "member_feedback", f"intro={iid}")
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()

    @app.get("/akun/profil")
    async def profile_form(request: Request):
        s = current(request)
        return profile_page(s, me(s)) if s else RedirectResponse("/akun/masuk", 303)

    @app.post("/akun/profil")
    async def profile_save(request: Request):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            m = me(s)
            posted = {k: str(form.get(k) or "") for k in EDITABLE}
            # reuse the registration validator: identity/consent fields are filled from the stored row, only EDITABLE errors count
            base = dict(full_name=m["full_name"], phone=m["phone"] or "0", email=m["email"] or "", password="x" * 12,
                        password2="x" * 12, gender=m["gender"], birth_date=m["birth_date"], source=m["source"] or "Lainnya",
                        program_id="1", ktp_nik_ok="on", ktp_addr_ok="on", ktp_sign_ok="on", consent_terms="on",
                        consent_privacy="on", consent_honest="on")
            clean, errors, _ = register.validate({**base, **posted})
            errors = {k: v for k, v in errors.items() if k in EDITABLE}
            if errors:
                return profile_page(s, m, 400, errors, posted)
            core = any(clean[k] != m[k] for k in CORE)
            cols = ", ".join(f"{k}=?" for k in EDITABLE)
            with db.tx(conn):
                conn.execute(f"UPDATE members SET {cols}, perlu_review=CASE WHEN ? OR ? THEN 1 ELSE perlu_review END WHERE id=?",
                             (*[clean[k] for k in EDITABLE], int(core), clean["perlu_review"], m["id"]))
                audit(m, "member_profile_edit", f"core={int(core)}")
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()

    @app.post("/akun/hapus")
    async def delete_req(request: Request):
        s, form, bad = await gate(request)
        if bad:
            return bad
        try:
            m = me(s)
            key, ip = "m:del:" + m["code"], register.client_ip(request)
            if auth.is_locked(conn, key, ip):
                return dashboard(s, 429, "Terlalu banyak percobaan. Coba lagi nanti.")
            pw = str(form.get("password") or "")
            if form.get("confirm") != "on" or len(pw) > 256 or not auth.verify_password(m["password_hash"] or auth.DUMMY_HASH, pw):
                auth.record_fail(conn, key, ip)
                return dashboard(s, 400, "Centang konfirmasi dan masukkan password yang benar.")
            auth.clear_fails(conn, key)
            with db.tx(conn):
                if conn.execute("INSERT OR IGNORE INTO deletion_requests(member_id,created_at) VALUES(?,?)",
                                (m["id"], db.now_iso())).rowcount:
                    audit(m, "member_delete_request")
            return RedirectResponse("/akun", 303)
        finally:
            await form.close()
