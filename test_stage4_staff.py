"""ACK Stage 4 tests: finance payment queue + Owner program config. Synthetic data only."""
import gc
import os
import pathlib
import re
import threading
import unittest
import warnings

import auth
import db
import test_stage2 as t2
import test_stage3 as t3
from fastapi.testclient import TestClient

PW = "dummy-staff-Passw0rd"
UP = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}


class S(t3.P):
    def setUp(self):
        super().setUp()
        self.w.execute("UPDATE programs SET price_idr=1500000 WHERE id=2")
        self.hash = auth.hash_password(PW)

    def staff(self, name, *roles):
        sid = self.w.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,'t')",
                             (name, name, self.hash)).lastrowid
        for r in roles:
            self.w.execute("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", (sid, r))
        cl = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        cl.__enter__()
        self.addCleanup(cl.__exit__)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', cl.get("/login").text).group(1)
        self.assertEqual(cl.post("/login", data={"username": name, "password": PW, "csrf_token": tok}).status_code, 303)
        return cl

    def stok(self, cl, path="/dashboard"):
        return re.search(r'name="csrf_token" value="([^"]+)"', cl.get(path).text).group(1)

    def spost(self, cl, path, data=None, csrf=True):
        d = dict(data or {})
        if csrf:
            d["csrf_token"] = self.stok(cl)
        return cl.post(path, data=d)

    def paying(self, duration=30, proof=True):
        """member with a pending invoice (+ uploaded proof); program 2 duration configurable."""
        self.w.execute("UPDATE programs SET duration_days=? WHERE id=2", (duration,))
        cl, mid = self.member(program="2")
        cl.get("/akun")
        if proof:
            self.assertEqual(self.post(cl, "/akun/bayar/unggah", files_=UP).status_code, 303)
        return cl, mid, self.pay(mid)["id"]

    def sub(self, mid):
        return self.w.execute("SELECT * FROM subscriptions WHERE member_id=?", (mid,)).fetchone()


class TestRoles(S):
    def test_role_matrix(self):
        _, _, pid = self.paying()
        member, _ = self.member()
        paths = ["/keuangan", f"/keuangan/{pid}/bukti", "/owner/program"]
        want = {"finance": [200, 200, 403], "owner": [200, 200, 200], "verifier": [403] * 3,
                "matchmaker": [403] * 3, "event": [403] * 3}
        for role, codes in want.items():
            cl = self.staff("u_" + role, role)
            self.assertEqual([cl.get(p).status_code for p in paths], codes, role)
        for p in paths:
            self.assertEqual(self.c.get(p).headers["location"], "/login")   # anon
            self.assertIn(member.get(p).status_code, (303, 403))            # member session is not staff
            self.assertNotEqual(member.get(p).status_code, 200)

    def test_posts_forbidden_for_other_roles_and_state_unchanged(self):
        _, mid, pid = self.paying()
        v = self.staff("ver", "verifier")
        self.assertEqual(self.spost(v, f"/keuangan/{pid}/setuju").status_code, 403)
        self.assertEqual(self.spost(v, f"/keuangan/{pid}/tolak", {"reason": "x"}).status_code, 403)
        self.assertEqual(self.spost(v, "/owner/program/2", {"price_idr": "1"}).status_code, 403)
        f = self.staff("fin", "finance")
        self.assertEqual(self.spost(f, "/owner/program/2", {"price_idr": "1"}).status_code, 403)
        self.assertEqual(self.pay(mid)["status"], "pending")
        self.assertEqual(self.w.execute("SELECT price_idr FROM programs WHERE id=2").fetchone()[0], 1500000)


class TestCsrf(S):
    def test_all_posts_need_csrf(self):
        _, mid, pid = self.paying()
        o = self.staff("own", "owner")
        for p, d in ((f"/keuangan/{pid}/setuju", {}), (f"/keuangan/{pid}/tolak", {"reason": "x"}),
                     ("/owner/program/2", {"duration_days": "5"})):
            self.assertEqual(self.spost(o, p, d, csrf=False).status_code, 403, p)
            self.assertEqual(o.post(p, data={**d, "csrf_token": "wrong"}).status_code, 403, p)
        self.assertEqual(self.pay(mid)["status"], "pending")
        self.assertEqual(self.w.execute("SELECT duration_days FROM programs WHERE id=2").fetchone()[0], 30)


class TestApprove(S):
    def test_approve_starts_now_ends_duration(self):
        _, mid, pid = self.paying(duration=45)
        f = self.staff("fin", "finance")
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/setuju").status_code, 303)
        s = self.sub(mid)
        self.assertEqual(s["status"], "active")
        from datetime import datetime, timedelta, timezone
        a = datetime.strptime(s["starts_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(datetime.strptime(s["ends_at"], "%Y-%m-%dT%H:%M:%SZ") - a, timedelta(days=45))
        self.assertLess(abs((datetime.now(timezone.utc).replace(tzinfo=None) - a).total_seconds()), 30)
        self.assertEqual(self.pay(mid)["status"], "approved")

    def test_double_approve_idempotent(self):
        _, mid, pid = self.paying()
        f = self.staff("fin", "finance")
        self.spost(f, f"/keuangan/{pid}/setuju")
        first = tuple(self.sub(mid))
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/setuju").status_code, 409)
        self.assertEqual(tuple(self.sub(mid)), first)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='payment_approve'").fetchone()[0], 1)

    def test_concurrent_approve_single_winner(self):
        _, mid, pid = self.paying()
        a, b = self.staff("fa", "finance"), self.staff("fb", "finance")
        toks = [self.stok(a), self.stok(b)]
        out, bar = [], threading.Barrier(2)

        def go(cl, t):
            bar.wait()
            r = cl.post(f"/keuangan/{pid}/setuju", data={"csrf_token": t}); out.append((r.status_code, r.headers.get("location"), r.text[:80]))
        th = [threading.Thread(target=go, args=x) for x in ((a, toks[0]), (b, toks[1]))]
        [t.start() for t in th]
        [t.join() for t in th]
        self.assertEqual(sorted(x[0] for x in out), [303, 409], out)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='payment_approve'").fetchone()[0], 1)

    def test_blocked_without_proof(self):
        _, mid, pid = self.paying(proof=False)
        f = self.staff("fin", "finance")
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/setuju").status_code, 409)
        self.assertEqual((self.pay(mid)["status"], self.sub(mid)["status"], self.sub(mid)["starts_at"]), ("pending", "pending", None))

    def test_blocked_when_duration_null_or_nonpositive(self):
        _, mid, pid = self.paying(duration=None)
        f = self.staff("fin", "finance")
        r = self.spost(f, f"/keuangan/{pid}/setuju")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Durasi", r.text)
        self.assertEqual((self.pay(mid)["status"], self.sub(mid)["starts_at"]), ("pending", None))
        # schema CHECK forbids <=0; a legacy/zero value must still be blocked by the handler
        self.w.execute("PRAGMA ignore_check_constraints=1")
        self.w.execute("UPDATE programs SET duration_days=0 WHERE id=2")
        self.w.execute("PRAGMA ignore_check_constraints=0")
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/setuju").status_code, 409)
        self.assertEqual(self.pay(mid)["status"], "pending")

    def test_only_pending(self):
        _, mid, pid = self.paying()
        f = self.staff("fin", "finance")
        self.w.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/setuju").status_code, 409)
        self.assertEqual(self.sub(mid)["starts_at"], None)
        self.assertEqual(self.spost(f, "/keuangan/99999/setuju").status_code, 404)


class TestReject(S):
    def test_reject_requires_reason_then_member_sees_it(self):
        cl, mid, pid = self.paying()
        f = self.staff("fin", "finance")
        for bad in ("", "   ", "x" * 301):
            self.assertEqual(self.spost(f, f"/keuangan/{pid}/tolak", {"reason": bad}).status_code, 400)
        self.assertEqual(self.pay(mid)["status"], "pending")
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/tolak", {"reason": "Nominal salah"}).status_code, 303)
        p = self.pay(mid)
        self.assertEqual((p["status"], p["reject_reason"]), ("rejected", "Nominal salah"))
        self.assertIn("Nominal salah", cl.get("/akun").text)
        self.assertEqual(self.sub(mid)["status"], "pending")
        self.assertEqual(self.spost(f, f"/keuangan/{pid}/tolak", {"reason": "lagi"}).status_code, 409)
        d = " ".join(r[0] for r in self.w.execute("SELECT detail FROM audit_log WHERE action='payment_reject'"))
        self.assertNotIn("Nominal", d)

    def test_rejected_leaves_queue_and_no_proof_cannot_reject(self):
        _, mid, pid = self.paying()
        _, _, nop = self.paying(proof=False)
        f = self.staff("fin", "finance")
        self.assertEqual(self.spost(f, f"/keuangan/{nop}/tolak", {"reason": "x"}).status_code, 409)
        self.spost(f, f"/keuangan/{pid}/tolak", {"reason": "x"})
        q = f.get("/keuangan").text
        self.assertNotIn(f"/keuangan/{pid}/bukti", q)


class TestProofPrivacy(S):
    def test_proof_decrypts_no_store_audit_id_only(self):
        _, mid, pid = self.paying()
        row = self.w.execute("SELECT f.stored_path,f.encrypted FROM payments p JOIN member_files f ON f.id=p.proof_file_id WHERE p.id=?", (pid,)).fetchone()
        self.assertEqual(row["encrypted"], 1)
        raw = t3.rb(os.path.join(self.priv, row["stored_path"]))
        f = self.staff("fin", "finance")
        r = f.get(f"/keuangan/{pid}/bukti")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotEqual(r.content, raw)                      # at rest is ciphertext
        self.assertTrue(r.content.startswith(b"\xff\xd8"))        # served decrypted (stored re-encoded JPEG)
        det = self.w.execute("SELECT detail FROM audit_log WHERE action='payment_proof_view'").fetchone()[0]
        self.assertEqual(det, f"payment={pid}")
        q = f.get("/keuangan").text
        self.assertNotIn("Budi", q)
        self.assertNotIn("08123", q)
        self.assertEqual(f.get("/keuangan/99999/bukti").status_code, 404)
        self.assertEqual(self.staff("ver2", "verifier").get(f"/keuangan/{pid}/bukti").status_code, 403)


class TestProgramConfig(S):
    def test_owner_sets_and_clears_values(self):
        o = self.staff("own", "owner")
        q = lambda: tuple(self.w.execute("SELECT price_idr,duration_days,intro_quota FROM programs WHERE id=2").fetchone())
        self.assertEqual(self.spost(o, "/owner/program/2", {"price_idr": "2000000", "duration_days": "90", "intro_quota": "0"}).status_code, 303)
        self.assertEqual(q(), (2000000, 90, 0))
        self.assertEqual(self.spost(o, "/owner/program/2", {"price_idr": "", "duration_days": "", "intro_quota": ""}).status_code, 303)
        self.assertEqual(q(), (None, None, None))      # blank = unconfigured, never 0/free
        self.assertIn("belum diatur", o.get("/owner/program").text)

    def test_invalid_values_rejected_nothing_written(self):
        o = self.staff("own", "owner")
        before = self.w.execute("SELECT price_idr,duration_days,intro_quota FROM programs WHERE id=2").fetchone()
        for d in ({"duration_days": "0"}, {"duration_days": "-5"}, {"duration_days": "abc"}, {"price_idr": "-1"},
                  {"intro_quota": "-1"}, {"price_idr": "1.5"}, {"price_idr": "1e3"}, {"duration_days": "99999"}):
            self.assertEqual(self.spost(o, "/owner/program/2", d).status_code, 400, d)
        self.assertEqual(self.spost(o, "/owner/program/999", {}).status_code, 404)
        self.assertEqual(tuple(self.w.execute("SELECT price_idr,duration_days,intro_quota FROM programs WHERE id=2").fetchone()), tuple(before))

    def test_audit_has_ids_numbers_only(self):
        o = self.staff("own", "owner")
        self.spost(o, "/owner/program/2", {"price_idr": "5", "duration_days": "7", "intro_quota": "1"})
        self.assertEqual(self.w.execute("SELECT detail FROM audit_log WHERE action='program_config'").fetchone()[0],
                         "program=2 price=5 days=7 quota=1")

    def test_no_env_writes(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "staff.py")) as fh:
            src = fh.read()
        self.assertNotIn("environ", src)
        self.assertNotIn(".env", src)


class TestNoResourceWarning(unittest.TestCase):
    def test_app_lifecycle_leaks_no_connection(self):
        import tempfile
        from app import create_app
        with warnings.catch_warnings(record=True) as w, tempfile.TemporaryDirectory() as d:
            warnings.simplefilter("always")
            with TestClient(create_app(db_path=os.path.join(d, "x.db"), secret_key=t2.SECRET, cookie_secure=False,
                                       fernet_key=t2.FKEY.decode())) as c:
                c.get("/health")
            gc.collect()
        self.assertEqual([str(x.message) for x in w if x.category is ResourceWarning], [])


if __name__ == "__main__":
    unittest.main()


class TestTableWrap(unittest.TestCase):
    def test_every_table_in_scroll_wrapper(self):
        import glob
        for f in glob.glob("templates/*.html"):
            s = pathlib.Path(f).read_text()
            self.assertEqual(s.count("<table>"), s.count("<div class=\"tw\"><table>"), f)
        b = pathlib.Path("templates/base.html").read_text()
        self.assertIn(".tw{overflow-x:auto;max-width:100%;min-width:0}", b)
