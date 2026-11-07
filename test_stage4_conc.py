"""Stage 4: shared-SQLite-connection concurrency. Staff approve (tx held open) vs member profile/login/register on other threads."""
import threading
import time
import unittest
from unittest import mock

import db
import test_stage2 as t2
import test_stage4_staff as ts
from fastapi.testclient import TestClient


class Conc(ts.S):
    def race(self, fail_approve=False):
        mcl, mid, pid = self.paying()
        f = self.staff("fin", "finance")
        ftok, ptok = self.stok(f), self.tok(mcl, "/akun/profil")
        m2 = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)   # separate loop thread
        m2.__enter__(); self.addCleanup(m2.__exit__)
        ltok = self.tok(m2, "/akun/masuk")
        m3 = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        m3.__enter__(); self.addCleanup(m3.__exit__)
        rtok = self.csrf(m3)
        real, inside, go = db.audit, threading.Event(), threading.Event()

        def slow_audit(conn, sid, action, detail=""):
            if action == "payment_approve":
                inside.set(); go.wait(5); time.sleep(0.2)          # hold the staff tx open while others hit the connection
                if fail_approve:
                    raise RuntimeError("injected fault")
            return real(conn, sid, action, detail)
        out = {}
        form = {"csrf_token": ptok, **{k: v for k, v in t2.fields(domicile="Medan").items() if k in
                ("domicile", "ethnicity", "religion", "marital_status", "education", "height_cm", "intro", "pref_age_min", "pref_age_max", "pref_religion", "pref_domicile", "pref_notes", "publish_consent")}}
        reg = t2.fields(phone="081299900001", email="race@example.test")

        def staff(): out["staff"] = f.post(f"/keuangan/{pid}/setuju", data={"csrf_token": ftok}).status_code

        def prof(): inside.wait(5); go.set(); r = mcl.post("/akun/profil", data=form); out["prof"] = r.status_code; out["ploc"] = r.headers.get("location")

        def login():
            inside.wait(5)
            out["login"] = m2.post("/akun/masuk", data={"csrf_token": ltok, "ident": "m1@example.test", "password": "dummy-Passw0rd-123"}).status_code

        def regi(): inside.wait(5); out["reg"] = self.submit(reg, client=m3, csrf=rtok).status_code
        with mock.patch.object(db, "audit", slow_audit):
            th = [threading.Thread(target=x) for x in (staff, prof, login, regi)]
            [t.start() for t in th]; [t.join(30) for t in th]
        return mid, pid, out

    def test_approve_vs_member_routes_commit(self):
        mid, pid, out = self.race()
        self.assertEqual((out["staff"], out["prof"], out["login"]), (303, 303, 303), out)
        self.assertIn(out["reg"], (200, 303), out)
        self.assertEqual(self.pay(mid)["status"], "approved")
        self.assertEqual(self.w.execute("SELECT domicile FROM members WHERE id=?", (mid,)).fetchone()[0], "Medan")
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members WHERE email_key='race@example.test'").fetchone()[0], 1)

    def test_fault_rolls_back_only_staff_work(self):
        mid, pid, out = self.race(fail_approve=True)
        self.assertEqual(out["staff"], 500, out)
        self.assertEqual((out["prof"], out["login"]), (303, 303), out)
        self.assertEqual((self.pay(mid)["status"], self.sub(mid)["status"], self.sub(mid)["starts_at"]), ("pending", "pending", None))
        self.assertEqual(self.w.execute("SELECT domicile FROM members WHERE id=?", (mid,)).fetchone()[0], "Medan")   # member commit not undone, not merged into staff tx
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='payment_approve'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
