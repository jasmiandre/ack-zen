"""ACK Stage 5 chunk 2: event payment proof + finance verification. Synthetic data only."""
import os
import re
import threading
import unittest
from unittest import mock

import db
import portal
import test_stage2 as t2
import test_stage5_event as t5

PNG = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}


class Proof(t5.E):
    def setUp(self):
        super().setUp()
        self.mk()
        self.e = self.eid()

    def selected(self, gender="Laki-laki"):
        cl, mid, _ = self.reg(gender)
        rid = self.rid(mid)
        self.assertEqual(self.spost(self.ev, f"/staff/event/{self.e}/pendaftar/{rid}/pilih").status_code, 200)
        return cl, mid, rid

    def up(self, cl, rid, files=PNG, csrf=True):
        d = {"rid": str(rid)}
        if csrf:
            d["csrf_token"] = self.tok(cl, "/akun")
        return cl.post("/akun/event/bukti", data=d, files=files)

    def status(self, rid):
        return self.w.execute("SELECT status FROM event_regs WHERE id=?", (rid,)).fetchone()[0]

    def uploaded(self):
        cl, mid, rid = self.selected()
        self.assertEqual(self.up(cl, rid).status_code, 303)
        return cl, mid, rid

    def test_upload_ok_encrypted_price_locked(self):
        cl, mid, rid = self.selected()
        self.w.execute("UPDATE events SET price_idr=999 WHERE id=?", (self.e,))   # later price change must not touch the snapshot
        self.assertEqual(self.up(cl, rid).status_code, 303)
        r = self.w.execute("SELECT * FROM event_regs WHERE id=?", (rid,)).fetchone()
        self.assertEqual((r["status"], r["locked_price_idr"]), ("proof_uploaded", 290000))
        f = self.w.execute("SELECT * FROM member_files WHERE id=?", (r["proof_file_id"],)).fetchone()
        self.assertEqual((f["kind"], f["encrypted"]), ("event_proof", 1))
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0], 0)
        page = cl.get(f"/akun/event/{rid}").text
        self.assertIn("290.000", page)
        self.assertNotRegex(page, r"(?i)rekening|jatuh tempo")

    def test_invalid_state_and_files(self):
        cl, mid, _ = self.reg("Laki-laki")
        rid = self.rid(mid)
        self.assertEqual(self.up(cl, rid).status_code, 409)   # registered, not selected
        self.spost(self.ev, f"/staff/event/{self.e}/pendaftar/{rid}/pilih")
        for files in ({"proof": ("x.png", b"not an image", "image/png")}, {"proof": ("x.png", b"", "image/png")},
                      {"other": PNG["proof"]}, {"proof": PNG["proof"], "extra": PNG["proof"]}):
            self.assertEqual(self.up(cl, rid, files).status_code, 400)
        self.assertEqual(self.status(rid), "selected")
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM member_files WHERE kind='event_proof'").fetchone()[0], 0)

    def test_csrf_and_idor(self):
        cl, mid, rid = self.selected("Laki-laki")
        self.assertEqual(self.up(cl, rid, csrf=False).status_code, 403)
        other, _, _ = self.reg("Perempuan")
        self.assertEqual(other.get(f"/akun/event/{rid}").status_code, 404)
        d = {"rid": str(rid), "csrf_token": self.tok(other, "/akun")}
        self.assertEqual(other.post("/akun/event/bukti", data=d, files=PNG).status_code, 404)
        self.assertEqual(other.post("/akun/event/bukti", data={**d, "rid": "abc"}, files=PNG).status_code, 404)
        self.assertEqual(self.status(rid), "selected")

    def test_null_price_never_uploadable(self):
        cl, mid, rid = self.selected()
        self.w.execute("UPDATE event_regs SET locked_price_idr=NULL WHERE id=?", (rid,))
        self.assertEqual(self.up(cl, rid).status_code, 409)

    def test_finance_roles_view_audit_nostore(self):
        _, mid, rid = self.uploaded()
        url = f"/keuangan/event/{rid}/bukti"
        for who, code in ((self.ev, 403), (self.fin, 200), (self.own, 200)):
            self.assertEqual(who.get(url).status_code, code)
            self.assertEqual(who.get("/keuangan/event").status_code, code)
        r = self.fin.get(url)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertTrue(r.content.startswith(b"\xff\xd8"))
        det = [x[0] for x in self.w.execute("SELECT detail FROM audit_log WHERE action='event_proof_view'")]
        self.assertTrue(det and all(d == f"reg={rid}" for d in det))
        self.assertEqual(self.fin.get(f"/keuangan/event/9999/bukti").status_code, 404)

    def test_approve_roles_csrf_idempotent(self):
        _, mid, rid = self.uploaded()
        u = f"/keuangan/event/{rid}/setuju"
        self.assertEqual(self.spost(self.ev, u).status_code, 403)
        self.assertEqual(self.spost(self.fin, u, csrf=False).status_code, 403)
        self.assertEqual(self.status(rid), "proof_uploaded")
        self.assertEqual(self.spost(self.fin, u).status_code, 200)
        self.assertEqual(self.status(rid), "paid")
        self.assertEqual(self.spost(self.fin, u).status_code, 409)
        self.assertEqual(self.spost(self.own, u).status_code, 409)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='event_payment_approve'").fetchone()[0], 1)

    def test_approve_concurrent_single_winner(self):
        _, mid, rid = self.uploaded()
        tok = {c: self.stok(c) for c in (self.fin, self.own)}
        codes, go = [], threading.Barrier(2)

        def run(c):
            go.wait()
            codes.append(c.post(f"/keuangan/event/{rid}/setuju", data={"csrf_token": tok[c]}).status_code)
        ts = [threading.Thread(target=run, args=(c,)) for c in tok]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(codes), [200, 409])
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='event_payment_approve'").fetchone()[0], 1)

    def test_reject_reason_keeps_seat_then_resubmit_cleans_old(self):
        cl, mid, rid = self.uploaded()
        u = f"/keuangan/event/{rid}/tolak"
        self.assertEqual(self.spost(self.fin, u, {"reason": "  "}).status_code, 400)
        self.assertEqual(self.spost(self.ev, u, {"reason": "x"}).status_code, 403)
        self.assertEqual(self.spost(self.fin, u, {"reason": "Buram"}).status_code, 200)
        r = self.w.execute("SELECT * FROM event_regs WHERE id=?", (rid,)).fetchone()
        self.assertEqual((r["status"], r["reject_reason"]), ("selected", "Buram"))
        self.assertEqual(self.fin.get(f"/keuangan/event/{rid}/bukti").status_code, 200)   # proof kept private until replaced
        self.assertIn("Buram", cl.get(f"/akun/event/{rid}").text)
        self.assertEqual(self.spost(self.fin, u, {"reason": "again"}).status_code, 409)
        old = self.w.execute("SELECT stored_path FROM member_files WHERE id=?", (r["proof_file_id"],)).fetchone()[0]
        self.assertEqual(self.up(cl, rid).status_code, 303)   # held seat still counts, replacement allowed
        r2 = self.w.execute("SELECT * FROM event_regs WHERE id=?", (rid,)).fetchone()
        self.assertEqual((r2["status"], r2["reject_reason"]), ("proof_uploaded", None))
        self.assertNotEqual(r2["proof_file_id"], r["proof_file_id"])
        self.assertFalse(os.path.exists(os.path.join(self.priv_dir(), old)))   # old file removed via durable queue
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0], 0)

    def priv_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.dbp)), "private")

    def test_db_failure_rolls_back_and_cleans_new_file(self):
        cl, mid, rid = self.selected()
        before = set(os.listdir(self.priv_dir()))
        real = db.audit

        def boom(conn, sid, action, *a):
            if action == "event_proof_upload":
                raise RuntimeError("injected")
            return real(conn, sid, action, *a)
        with mock.patch.object(db, "audit", boom):
            self.assertEqual(self.up(cl, rid).status_code, 500)
        self.assertEqual(self.status(rid), "selected")
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM member_files WHERE kind='event_proof'").fetchone()[0], 0)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0], 0)
        self.assertEqual(set(os.listdir(self.priv_dir())), before)   # no orphan file

    def test_unlink_failure_stays_queued(self):
        cl, mid, rid = self.uploaded()
        self.spost(self.fin, f"/keuangan/event/{rid}/tolak", {"reason": "x"})
        with mock.patch("os.unlink", side_effect=PermissionError("denied")):
            self.assertEqual(self.up(cl, rid).status_code, 303)
        self.assertEqual(self.status(rid), "proof_uploaded")   # commit survived the cleanup failure
        row = self.w.execute("SELECT attempts,reason FROM pending_file_cleanup").fetchone()
        self.assertEqual((row["attempts"], row["reason"]), (1, "replaced"))
        self.assertEqual(portal.reconcile_pending(self.w, self.priv_dir()), 1)

    def test_paid_cancel_blocked(self):
        _, mid, rid = self.uploaded()
        self.spost(self.fin, f"/keuangan/event/{rid}/setuju")
        r = self.spost(self.ev, f"/staff/event/{self.e}/pendaftar/{rid}/batal")
        self.assertEqual(r.status_code, 409)
        self.assertIn("refund", r.text)
        self.assertEqual(self.status(rid), "paid")


for _n in [n for n in dir(t5.E) if n.startswith("test_")]:   # inherited chunk-1 tests run in their own module
    setattr(Proof, _n, None)

if __name__ == "__main__":
    unittest.main()
