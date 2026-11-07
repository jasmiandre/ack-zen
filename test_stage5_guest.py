"""ACK Stage 5 chunk 3: guest event registration. Synthetic data only."""
import os
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import events
import register
import test_stage2 as t2
import test_stage4_staff as t4


def gf(**o):
    d = dict(full_name="Tamu Dummy", phone="081299990001", email="tamu1@example.test", birth_date="1995-05-17", gender="Perempuan",
             password="dummy-Passw0rd-123", password2="dummy-Passw0rd-123", slug="dinner-demo",
             ktp_nik_ok="on", ktp_addr_ok="on", ktp_sign_ok="on", consent_terms="on", consent_privacy="on", consent_honest="on")
    d.update(o)
    return {k: v for k, v in d.items() if v is not None}


def gfiles(**o):
    f = {"ktp_redacted": ("k.jpg", t2.img_bytes(), "image/jpeg"), "photo_face": ("f.jpg", t2.img_bytes(exif=True), "image/jpeg")}
    f.update(o)
    return {k: v for k, v in f.items() if v is not None}


class G(t4.S):
    def setUp(self):
        super().setUp()
        self.w.execute("INSERT INTO events(title,slug,starts_at,capacity,quota_m,quota_f,price_idr,active,created_at,extra_fields) "
                       "VALUES('Dinner','dinner-demo','2026-12-01T12:00',2,1,1,100000,1,'t','[]')")

    def post(self, f=None, fl=None, cl=None, csrf=None):
        cl = cl or TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        if csrf is None:
            r = cl.get("/event/dinner-demo/tamu")
            self.assertEqual(r.status_code, 200)
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
        return cl.post("/event/tamu/submit", data=gf() if f is None else f, files=gfiles() if fl is None else fl,
                       headers={"X-CSRF-Token": csrf})

    def rd(self, n):
        with open(os.path.join(self.priv, n), "rb") as fh:
            return fh.read()

    def cnt(self, t):
        return self.w.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    def test_happy_no_subscription_registered_not_selected(self):
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        m = self.w.execute("SELECT * FROM members").fetchone()
        self.assertEqual((m["status"], m["gender"], m["height_cm"], m["religion"], m["pref_age_min"]), ("pending", "Perempuan", None, None, None))
        self.assertTrue(m["password_hash"].startswith("$argon2"))
        self.assertEqual(self.cnt("subscriptions"), 0)
        self.assertEqual(self.cnt("payments"), 0)
        self.assertEqual(self.w.execute("SELECT status,selected_at FROM event_regs").fetchone()[:], ("registered", None))
        kinds = {x[0]: x[1] for x in self.w.execute("SELECT kind,encrypted FROM member_files")}
        self.assertEqual(kinds, {"ktp_redacted": 1, "photo_face": 0})
        for (p,) in self.w.execute("SELECT stored_path FROM member_files"):
            self.assertTrue(os.path.exists(os.path.join(self.priv, p)))
        # logged in as the new member; appears in verifier queue only after owner/verifier role sees it
        v = self.staff("ver", "verifier")
        self.assertIn(m["code"], v.get("/verifikasi").text)
        self.assertNotIn("event_proof", str(kinds))

    def test_login_works_with_guest_password(self):
        self.post()
        cl = TestClient(self.app, follow_redirects=False)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', cl.get("/akun/masuk").text).group(1)
        r = cl.post("/akun/masuk", data={"ident": "tamu1@example.test", "password": "dummy-Passw0rd-123", "csrf_token": tok})
        self.assertEqual(r.status_code, 303)

    def test_existing_account_no_takeover_no_dup(self):
        _, mid = self.member()
        h = self.w.execute("SELECT password_hash,full_name FROM members WHERE id=?", (mid,)).fetchone()
        phone, email = self.w.execute("SELECT phone,email FROM members WHERE id=?", (mid,)).fetchone()
        for over in ({"phone": phone, "email": "new@example.test"}, {"email": email.upper(), "phone": "081299990002"}):
            r = self.post(gf(**over))
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["login"], "/akun/masuk")
            self.assertNotIn("set-cookie", {k.lower() for k in r.headers if "session" in r.headers[k]})
        self.assertEqual(self.cnt("members"), 1)
        self.assertEqual(self.cnt("event_regs"), 0)
        self.assertEqual(self.w.execute("SELECT password_hash,full_name FROM members WHERE id=?", (mid,)).fetchone()[:], tuple(h))
        self.assertEqual(len(os.listdir(self.priv)), 3)   # only the original member's files

    def test_inactive_or_unknown_event(self):
        self.w.execute("UPDATE events SET active=0")
        self.assertEqual(TestClient(self.app).get("/event/dinner-demo/tamu").status_code, 404)
        cl = TestClient(self.app)
        r = cl.get("/event/dinner-demo/tamu")
        self.w.execute("UPDATE events SET active=1")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', cl.get("/event/dinner-demo/tamu").text).group(1)
        self.w.execute("UPDATE events SET active=0")
        r = self.post(cl=cl, csrf=csrf)
        self.assertEqual(r.status_code, 404)
        self.assertEqual((self.cnt("members"), self.cnt("event_regs")), (0, 0))
        self.assertFalse(os.path.isdir(self.priv) and [x for x in os.listdir(self.priv) if x.endswith((".enc", ".jpg"))])

    def test_age_and_validation(self):
        for dob in ("2099-01-01", "2012-01-01", "2026-13-40", "1995-05"):
            self.assertEqual(self.post(gf(birth_date=dob)).status_code, 400, dob)
        self.assertEqual(self.post(gf(password2="x")).status_code, 400)
        self.assertEqual(self.post(gf(gender="X")).status_code, 400)
        self.assertEqual(self.post(gf(ktp_nik_ok=None)).status_code, 400)
        self.assertEqual(self.cnt("members"), 0)

    def test_csrf_and_rate_limit(self):
        self.assertEqual(self.post(csrf="bad", cl=TestClient(self.app)).status_code, 403)
        self.assertEqual(TestClient(self.app).post("/event/tamu/submit", data=gf(), files=gfiles()).status_code, 403)
        with mock.patch.object(register, "SUBMIT_LIMIT", 2):
            codes = [self.post(gf(birth_date="2012-01-01")).status_code for _ in range(3)]
        self.assertEqual(codes, [400, 400, 429])

    def test_upload_privacy(self):
        self.assertEqual(self.post(fl=gfiles(ktp=("o.jpg", t2.img_bytes(), "image/jpeg"))).status_code, 400)   # original KTP field
        self.assertEqual(self.post(fl=gfiles(photo_body=("b.jpg", t2.img_bytes(), "image/jpeg"))).status_code, 400)
        self.assertEqual(self.post(fl=gfiles(photo_face=("f.jpg", b"notimage", "image/jpeg"))).status_code, 400)
        self.assertEqual(self.post(fl=gfiles(photo_face=None)).status_code, 400)
        self.assertEqual(self.cnt("members"), 0)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(TestClient(self.app).get("/static/js/guest.js").status_code, 200)
        for (p,) in self.w.execute("SELECT stored_path FROM member_files"):
            self.assertEqual(TestClient(self.app).get(f"/static/{p}").status_code, 404)
        face = [p for (p,) in self.w.execute("SELECT stored_path FROM member_files WHERE kind='photo_face'")][0]
        raw = self.rd(face)
        self.assertNotIn(b"SyntheticCam", raw)   # EXIF stripped
        ktp = [p for (p,) in self.w.execute("SELECT stored_path FROM member_files WHERE kind='ktp_redacted'")][0]
        self.assertNotEqual(self.rd(ktp)[:2], b"\xff\xd8")   # Fernet, not plain JPEG
        # finance/event staff cannot open the files; verifier can
        self.assertEqual(self.staff("fin2", "finance").get("/verifikasi/1/berkas/ktp_redacted").status_code, 403)
        self.assertEqual(self.staff("ver2", "verifier").get("/verifikasi/1/berkas/ktp_redacted").status_code, 200)

    def test_db_failure_atomic_and_files_removed(self):
        with mock.patch.object(events.db, "next_member_code", side_effect=RuntimeError("boom")):
            r = self.post()
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("boom", r.text)
        self.assertEqual((self.cnt("members"), self.cnt("member_files"), self.cnt("event_regs")), (0, 0, 0))
        self.assertEqual([x for x in os.listdir(self.priv)], [])

    def test_regs_failure_rolls_back_member(self):
        self.w.execute("CREATE TRIGGER boom BEFORE INSERT ON event_regs BEGIN SELECT RAISE(ABORT,'x'); END")
        self.assertEqual(self.post().status_code, 500)
        self.assertEqual((self.cnt("members"), self.cnt("member_files")), (0, 0))
        self.assertEqual(os.listdir(self.priv), [])

    def test_stage2_register_unchanged_and_form_link(self):
        self.assertIn("/event/dinner-demo/tamu", TestClient(self.app).get("/event/dinner-demo").text)
        self.assertIn("guest.js", TestClient(self.app).get("/event/dinner-demo/tamu").text)


if __name__ == "__main__":
    unittest.main()
