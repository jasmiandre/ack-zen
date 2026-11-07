"""ACK Stage 4 verification queue + KTP retention tests. Synthetic data only."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import staff
import test_stage4_staff as ts
import db


class V(ts.S):
    ALL = {"name": "1", "age": "1", "face": "1", "censor": "1"}

    def setUp(self):
        super().setUp()
        self.ver = self.staff("ver", "verifier")
        self.own = self.staff("own", "owner")

    def vpost(self, cl, path, data=None):
        return self.spost(cl, path, data)

    def approve(self, mid, cl=None):
        return self.vpost(cl or self.ver, f"/verifikasi/{mid}/setuju", self.ALL)

    def files(self, mid):
        return self.w.execute("SELECT * FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (mid,)).fetchone()

    def age_approval(self, mid, days, secs=0):
        at = (datetime.now(timezone.utc) - timedelta(days=days, seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.w.execute("UPDATE verifications SET approved_at=? WHERE member_id=? AND kind='ktp'", (at, mid))

    def approved(self, days=31):
        _, mid = self.member()
        self.assertEqual(self.approve(mid).status_code, 303)
        self.age_approval(mid, days)
        return mid


class TestVerify(V):
    def test_roles(self):
        _, mid = self.member()
        fin = self.staff("fin", "finance")
        for p in ("/verifikasi", f"/verifikasi/{mid}", f"/verifikasi/{mid}/berkas/ktp_redacted"):
            self.assertEqual(fin.get(p).status_code, 403)
            self.assertEqual(self.c.get(p).headers["location"], "/login")
            self.assertEqual(self.ver.get(p).status_code, 200)
            self.assertEqual(self.own.get(p).status_code, 200)
        self.assertEqual(self.spost(fin, f"/verifikasi/{mid}/setuju", self.ALL).status_code, 403)
        self.assertEqual(self.staff("fv", "finance", "verifier").get("/verifikasi").status_code, 200)

    def test_detail_fields_and_audit_no_pii(self):
        _, mid = self.member()
        t = self.ver.get(f"/verifikasi/{mid}").text
        for s in ("Budi Dummy", "Jakarta Barat", "pending", "ktp_redacted", "photo_face", "photo_body"):
            self.assertIn(s, t)
        self.assertEqual(self.ver.get(f"/verifikasi/{mid}/berkas/photo_face").status_code, 200)
        self.assertEqual(self.ver.get(f"/verifikasi/{mid}/berkas/../x").status_code, 404)
        self.assertEqual(self.ver.get(f"/verifikasi/{mid}/berkas/ktp").status_code, 404)
        r = self.ver.get(f"/verifikasi/{mid}/berkas/ktp_redacted")
        self.assertEqual((r.status_code, r.headers["cache-control"]), (200, "no-store"))
        fid = self.files(mid)["id"]
        a = self.w.execute("SELECT * FROM audit_log WHERE action='ktp_open'").fetchall()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["detail"], f"member={mid} file={fid}")
        self.assertIsNotNone(a[0]["staff_id"])
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='ktp_open'").fetchone()[0], 1)  # photos not KTP

    def test_approve_requires_every_check(self):
        _, mid = self.member()
        for k in self.ALL:
            d = {x: "1" for x in self.ALL if x != k}
            self.assertEqual(self.vpost(self.ver, f"/verifikasi/{mid}/setuju", d).status_code, 400)
        self.assertEqual(self.w.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()[0], "pending")

    def test_approve_sets_now_not_upload_and_no_activation(self):
        _, mid, pid = self.paying()
        self.w.execute("UPDATE member_files SET created_at='2001-01-01T00:00:00Z' WHERE member_id=?", (mid,))
        self.assertEqual(self.approve(mid).status_code, 303)
        v = self.w.execute("SELECT * FROM verifications WHERE member_id=? AND kind='ktp'", (mid,)).fetchone()
        self.assertEqual(v["status"], "approved")
        self.assertGreater(v["approved_at"], "2020")
        self.assertIn('"face": true', v["checklist"])
        self.assertIsNotNone(v["reviewer_id"])
        self.assertEqual(self.w.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()[0], "verified")
        self.assertEqual(self.sub(mid)["status"], "pending")          # payments separate: no activation
        self.assertEqual(self.pay(mid)["status"], "pending")
        self.assertEqual(self.approve(mid).status_code, 409)           # not in queue anymore

    def test_reject_requires_reason(self):
        _, mid = self.member()
        self.assertEqual(self.vpost(self.ver, f"/verifikasi/{mid}/tolak", {"reason": " "}).status_code, 400)
        self.assertEqual(self.vpost(self.ver, f"/verifikasi/{mid}/tolak", {"reason": "x" * 301}).status_code, 400)
        self.assertEqual(self.vpost(self.ver, f"/verifikasi/{mid}/tolak", {"reason": "buram"}).status_code, 303)
        self.assertEqual(self.w.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()[0], "rejected")
        v = self.w.execute("SELECT * FROM verifications WHERE member_id=? AND kind='ktp'", (mid,)).fetchone()
        self.assertEqual((v["status"], v["note"], v["approved_at"]), ("rejected", "buram", None))
        self.assertNotIn("buram", " ".join(r[0] for r in self.w.execute("SELECT detail FROM audit_log")))

    def test_csrf(self):
        _, mid = self.member()
        self.assertEqual(self.spost(self.ver, f"/verifikasi/{mid}/setuju", self.ALL, csrf=False).status_code, 403)
        self.assertEqual(self.spost(self.own, "/owner/ktp-retensi/hapus", {"confirm": "HAPUS"}, csrf=False).status_code, 403)


class TestRetention(V):
    def test_owner_only_and_default(self):
        self.assertEqual(self.w.execute("SELECT value FROM app_settings WHERE key='ktp_retention_days'").fetchone()[0], "30")
        fin = self.staff("fin", "finance")
        for cl in (self.ver, fin):
            self.assertEqual(cl.get("/owner/ktp-retensi").status_code, 403)
            self.assertEqual(self.spost(cl, "/owner/ktp-retensi/hapus", {"confirm": "HAPUS"}).status_code, 403)
            self.assertEqual(self.spost(cl, "/owner/ktp-retensi/hari", {"days": "5"}).status_code, 403)
        self.assertEqual(self.own.get("/owner/ktp-retensi").status_code, 200)

    def test_config_validation(self):
        for bad in ("", "0", "-1", "abc", "3651", "1.5"):
            self.assertEqual(self.spost(self.own, "/owner/ktp-retensi/hari", {"days": bad}).status_code, 400, bad)
        self.assertEqual(self.spost(self.own, "/owner/ktp-retensi/hari", {"days": "7"}).status_code, 303)
        self.assertEqual(self.w.execute("SELECT value FROM app_settings WHERE key='ktp_retention_days'").fetchone()[0], "7")

    def test_get_never_deletes_and_lists_due(self):
        mid = self.approved()
        f = self.files(mid)
        t = self.own.get("/owner/ktp-retensi").text
        self.assertIn(f"member {mid} / berkas {f['id']}", t)
        self.own.get("/dashboard"); self.own.get("/owner/ktp-retensi")
        self.assertTrue(os.path.exists(os.path.join(self.priv, f["stored_path"])))
        self.assertIsNotNone(self.files(mid))

    def run_del(self, confirm="HAPUS"):
        return self.spost(self.own, "/owner/ktp-retensi/hapus", {"confirm": confirm})

    def test_requires_confirm(self):
        mid = self.approved()
        self.assertEqual(self.run_del("").status_code, 400)
        self.assertIsNotNone(self.files(mid))

    def test_delete_preserves_verification_and_only_approved(self):
        mid = self.approved()
        _, rej = self.member(); self.vpost(self.ver, f"/verifikasi/{rej}/tolak", {"reason": "x"})
        _, unr = self.member()
        for m in (rej, unr):
            self.w.execute("UPDATE member_files SET created_at='2001-01-01T00:00:00Z' WHERE member_id=?", (m,))
        self.age_approval(rej, 99)
        f = self.files(mid); before = dict(self.w.execute("SELECT * FROM verifications WHERE member_id=? AND kind='ktp'", (mid,)).fetchone())
        path = os.path.join(self.priv, f["stored_path"]); self.assertTrue(os.path.exists(path))
        self.assertEqual(self.run_del().status_code, 200)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(self.files(mid))
        self.assertEqual(before, dict(self.w.execute("SELECT * FROM verifications WHERE member_id=? AND kind='ktp'", (mid,)).fetchone()))
        self.assertEqual(self.w.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()[0], "verified")
        self.assertIsNotNone(self.files(rej)); self.assertIsNotNone(self.files(unr))
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0], 0)
        self.assertTrue(self.w.execute("SELECT 1 FROM audit_log WHERE action='ktp_retention_delete' AND detail=?", (f"member={mid} file={f['id']}",)).fetchone())

    def test_boundary_day30(self):
        _, m29 = self.member(); _, m30 = self.member()
        self.approve(m29); self.approve(m30)
        self.age_approval(m29, 29, 86399); self.age_approval(m30, 30, 5)   # just under / just over 30 days
        t = self.own.get("/owner/ktp-retensi").text
        self.assertIn(f"member {m30} /", t); self.assertNotIn(f"member {m29} /", t)

    def test_batch_max_20(self):
        mids = [self.approved() for _ in range(22)]
        self.run_del()
        left = [m for m in mids if self.files(m)]
        self.assertEqual(len(left), 2)
        self.run_del()
        self.assertFalse([m for m in mids if self.files(m)])

    def test_unlink_failure_retained_and_retry(self):
        mid = self.approved(); f = self.files(mid)
        with mock.patch("portal.os.unlink", side_effect=PermissionError):
            self.assertEqual(self.run_del().status_code, 200)
        row = self.w.execute("SELECT * FROM pending_file_cleanup").fetchone()
        self.assertEqual((row["stored_path"], row["attempts"], row["last_error"]), (f["stored_path"], 1, "PermissionError"))
        self.assertTrue(os.path.exists(os.path.join(self.priv, f["stored_path"])))
        self.assertEqual(self.run_del().status_code, 200)   # nothing due, queue left for existing reconcile
        portal = __import__("portal")
        portal.reconcile_pending(self.w, self.priv)
        self.assertFalse(os.path.exists(os.path.join(self.priv, f["stored_path"])))

    def test_db_failure_rolls_back_without_unlink(self):
        mid = self.approved(); f = self.files(mid)
        self.w.execute("CREATE TRIGGER boom BEFORE INSERT ON pending_file_cleanup BEGIN SELECT RAISE(ABORT,'x'); END")
        self.assertEqual(self.run_del().status_code, 500)
        self.assertIsNotNone(self.files(mid))
        self.assertTrue(os.path.exists(os.path.join(self.priv, f["stored_path"])))
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='ktp_retention_delete'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
