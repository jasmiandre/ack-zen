"""ACK Stage 3 tests: member portal /akun. Synthetic data only."""
import os
import re
import sqlite3
import unittest
from unittest import mock
from datetime import date

from fastapi.testclient import TestClient

import auth
import db
import portal
import register
import test_stage2 as t2
from app import create_app

BANK = "BCA 000-000-0000 a.n. DUMMY ACK|Berita: kode member"


def rb(path):
    with open(path, "rb") as fh:
        return fh.read()


class P(t2.Base):
    """Base: registers members through the real /daftar flow; second connection `w` for staff-side state."""

    def setUp(self):
        super().setUp()
        self.w = sqlite3.connect(self.dbp, isolation_level=None, timeout=30)
        self.w.row_factory = sqlite3.Row
        self.addCleanup(self.w.close)
        self.n_members = 0

    def reopen(self, **kw):
        self.c.__exit__()
        self.app = create_app(db_path=self.dbp, secret_key=t2.SECRET, cookie_secure=False,
                              fernet_key=t2.FKEY.decode(), **kw)
        self.c = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        self.c.__enter__()

    def member(self, program="1", **over):
        """Register a new member; returns (client logged in, member_id)."""
        self.n_members += 1
        i = self.n_members
        cl = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        cl.__enter__()
        self.addCleanup(cl.__exit__)
        f = t2.fields(phone=f"08123000{i:04d}", email=f"m{i}@example.test", program_id=program, **over)
        r = self.submit(f, client=cl)
        self.assertEqual(r.status_code, 200, r.text)
        mid = self.w.execute("SELECT id FROM members WHERE email_key=?", (f["email"],)).fetchone()[0]
        return cl, mid

    def tok(self, cl, path="/akun"):
        r = cl.get(path)
        self.assertEqual(r.status_code, 200, (path, r.status_code))
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertTrue(m, path)
        return m.group(1)

    def post(self, cl, path, data=None, files_=None, csrf=True, back="/akun"):
        d = dict(data or {})
        if csrf:
            d["csrf_token"] = self.tok(cl, back)
        return cl.post(path, data=d, files=files_ or None)

    def priced_program(self, price=1_500_000):
        self.w.execute("UPDATE programs SET price_idr=? WHERE id=2", (price,))

    def intro(self, a, b, reason="Sama-sama suka padel.", status="proposed", ra=None, rb=None):
        return self.w.execute(
            "INSERT INTO introductions(member_a,member_b,status,created_at,resp_a,resp_b,match_reason) VALUES(?,?,?,?,?,?,?)",
            (a, b, status, db.now_iso(), ra, rb, reason)).lastrowid

    def pay(self, mid):
        return self.w.execute("SELECT p.* FROM payments p JOIN subscriptions s ON s.id=p.subscription_id "
                              "WHERE s.member_id=? ORDER BY p.id DESC", (mid,)).fetchone()


class TestTimeline(unittest.TestCase):
    def test_states(self):
        f = portal.timeline
        names = [s[0] for s in f(False, False, False, False)]
        self.assertEqual(names, ["Pendaftaran", "Pembayaran", "Verifikasi", "Aktif", "Perkenalan"])
        st = lambda *a: [s[1] for s in f(*a)]
        self.assertEqual(st(False, False, False, False), ["done", "current", "todo", "todo", "todo"])
        self.assertEqual(st(True, False, False, False), ["done", "done", "current", "todo", "todo"])
        self.assertEqual(st(True, True, True, False), ["done", "done", "done", "done", "current"])
        self.assertEqual(st(True, True, True, True), ["done"] * 5)


class TestPortalBasics(P):
    def test_anon_redirected_and_post_requires_session(self):
        self.assertEqual(self.c.get("/akun").headers["location"], "/akun/masuk")
        for p in ("/akun/bayar/unggah", "/akun/verifikasi/unggah", "/akun/hapus", "/akun/profil"):
            r = self.c.post(p, data={})
            self.assertIn(r.status_code, (303, 403), p)
            self.assertNotEqual(r.status_code, 200)

    def test_status_timeline_page(self):
        cl, _ = self.member()
        r = cl.get("/akun")
        self.assertEqual(r.status_code, 200)
        for w in ("Pendaftaran", "Pembayaran", "Verifikasi", "Aktif", "Perkenalan"):
            self.assertIn(w, r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])

    def test_subpage_has_css_csp(self):
        cl, _ = self.member()
        r = cl.get("/akun/profil")
        self.assertEqual(r.status_code, 200)
        self.assertIn("style-src 'self'", r.headers["content-security-policy"])

    def test_login_phone_and_email(self):
        cl, _ = self.member()
        for ident in ("0812-3000-0001", "M1@Example.test"):
            c2 = TestClient(self.app, follow_redirects=False)
            tok = self.tok(c2, "/akun/masuk")
            r = c2.post("/akun/masuk", data={"csrf_token": tok, "ident": ident, "password": "dummy-Passw0rd-123"})
            self.assertEqual(r.status_code, 303, ident)
            self.assertEqual(c2.get("/akun").status_code, 200)

    def test_staff_session_is_not_member(self):
        self.w.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES('s1','S',?,?)",
                       (auth.hash_password("x" * 14), db.now_iso()))
        self.w.execute("INSERT INTO staff_roles(staff_id,role) VALUES(1,'owner')")
        tok, _, _ = auth.create_session(self.w, t2.SECRET, 1)
        c2 = TestClient(self.app, follow_redirects=False, cookies={auth.COOKIE: tok})
        self.assertEqual(c2.get("/akun").status_code, 303)
        cl, _ = self.member()
        cl.cookies.set(auth.COOKIE, cl.cookies.get(auth.MEMBER_COOKIE))   # member token replayed as staff cookie
        self.assertEqual(cl.get("/dashboard").status_code, 303)


class TestPayment(P):
    def test_unconfigured_price_no_invoice(self):
        cl, _ = self.member()
        r = cl.get("/akun")
        self.assertIn("info harga via admin", r.text.lower())
        self.assertNotIn("Lunas", r.text)
        self.assertNotIn("Rp 0", r.text)

    def test_unique_code_deadline_bank_and_stable(self):
        self.priced_program()
        self.reopen(bank_info=BANK)
        cl, mid = self.member(program="2")
        r = cl.get("/akun")
        p = self.pay(mid)
        self.assertTrue(1 <= p["unique_code"] <= 999)
        self.assertIsNone(p["due_at"])                      # no approved deadline source -> NULL, never a made-up 48h
        self.assertIn("belum ditetapkan", r.text)
        self.assertIn("Rp " + f"{1_500_000 + p['unique_code']:,}".replace(",", "."), r.text)
        self.assertIn("Menunggu Bayar", r.text)
        self.assertIn("BCA 000-000-0000", r.text)
        self.assertIn("<br>", r.text)                      # '|' separates bank lines
        code = p["unique_code"]
        cl.get("/akun")
        self.assertEqual(self.pay(mid)["unique_code"], code)
        self.assertEqual(self.pay(mid)["amount_idr"], 1_500_000)   # base amount never mutated

    def test_bank_unconfigured_message(self):
        self.priced_program()
        cl, _ = self.member(program="2")
        self.assertIn("rekening belum diatur", cl.get("/akun").text.lower())

    def test_proof_upload_flow_and_encryption(self):
        self.priced_program()
        cl, mid = self.member(program="2")
        cl.get("/akun")
        up = {"proof": ("bukti.png", t2.img_bytes("PNG", (50, 40)), "image/png")}
        r = self.post(cl, "/akun/bayar/unggah", files_=up)
        self.assertEqual(r.status_code, 303)
        p = self.pay(mid)
        self.assertEqual(p["status"], "pending")
        self.assertTrue(p["proof_file_id"])
        self.assertIn("Menunggu Dicek", cl.get("/akun").text)
        f = self.w.execute("SELECT * FROM member_files WHERE id=?", (p["proof_file_id"],)).fetchone()
        self.assertEqual(f["encrypted"], 1)
        raw = rb(os.path.join(self.priv, f["stored_path"]))
        self.assertFalse(raw.startswith(b"\xff\xd8"))        # not a plain JPEG on disk
        # replace: old file row + disk file gone
        self.post(cl, "/akun/bayar/unggah", files_=up)
        self.assertFalse(os.path.exists(os.path.join(self.priv, f["stored_path"])))
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM member_files WHERE kind='payment_proof'").fetchone()[0], 1)

    def test_proof_rejects_bad_file_and_missing_csrf(self):
        self.priced_program()
        cl, mid = self.member(program="2")
        cl.get("/akun")
        r = self.post(cl, "/akun/bayar/unggah", files_={"proof": ("x.png", b"not an image", "image/png")})
        self.assertEqual(r.status_code, 400)
        r = self.post(cl, "/akun/bayar/unggah", csrf=False,
                      files_={"proof": ("b.png", t2.img_bytes("PNG"), "image/png")})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.pay(mid)["proof_file_id"])

    def test_approved_locks_and_rejected_reupload(self):
        self.priced_program()
        cl, mid = self.member(program="2")
        cl.get("/akun")
        up = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}
        self.w.execute("UPDATE payments SET status='rejected', reject_reason='Nominal tidak sesuai' WHERE id=?", (self.pay(mid)["id"],))
        r = cl.get("/akun")
        self.assertIn("Ditolak", r.text)
        self.assertIn("Nominal tidak sesuai", r.text)
        self.assertEqual(self.post(cl, "/akun/bayar/unggah", files_=up).status_code, 303)
        p = self.pay(mid)
        self.assertEqual((p["status"], p["reject_reason"]), ("pending", None))
        self.w.execute("UPDATE payments SET status='approved' WHERE id=?", (p["id"],))
        self.assertIn("Lunas", cl.get("/akun").text)
        self.assertEqual(self.post(cl, "/akun/bayar/unggah", files_=up).status_code, 400)

    def test_upload_without_payment_and_isolation(self):
        self.priced_program()
        a, ma = self.member(program="2")
        b, mb = self.member()              # no invoice (price NULL program 1)
        a.get("/akun")
        up = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}
        self.assertEqual(self.post(b, "/akun/bayar/unggah", files_=up).status_code, 400)
        self.assertIsNone(self.pay(ma)["proof_file_id"])


class TestVerification(P):
    def reject(self, mid, kind, why="Foto buram"):
        self.w.execute("INSERT INTO verifications(member_id,kind,status,note,created_at) VALUES(?,?,'rejected',?,?)",
                       (mid, kind, why, db.now_iso()))

    def test_pending_default_and_reason_shown(self):
        cl, mid = self.member()
        r = cl.get("/akun")
        self.assertIn("KTP", r.text)
        self.assertNotIn("Unggah ulang", r.text)
        self.reject(mid, "ktp")
        r = cl.get("/akun")
        self.assertIn("Foto buram", r.text)
        self.assertIn("Unggah ulang", r.text)

    def test_reupload_ktp_resets_and_replaces(self):
        cl, mid = self.member()
        old = self.w.execute("SELECT * FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (mid,)).fetchone()
        self.reject(mid, "ktp")
        r = self.post(cl, "/akun/verifikasi/unggah", {"kind": "ktp"},
                      {"ktp_redacted": ("k.jpg", t2.img_bytes(color=(1, 2, 3)), "image/jpeg")})
        self.assertEqual(r.status_code, 303)
        v = self.w.execute("SELECT status,note FROM verifications WHERE member_id=? AND kind='ktp'", (mid,)).fetchone()
        self.assertEqual((v["status"], v["note"]), ("pending", None))
        rows = self.w.execute("SELECT * FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (mid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], old["id"])
        self.assertEqual(rows[0]["encrypted"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.priv, old["stored_path"])))

    def test_reupload_foto_and_guards(self):
        cl, mid = self.member()
        img = {"photo_face": ("f.jpg", t2.img_bytes(), "image/jpeg")}
        # not rejected -> refused
        self.assertEqual(self.post(cl, "/akun/verifikasi/unggah", {"kind": "foto"}, img).status_code, 400)
        self.reject(mid, "foto")
        self.assertEqual(self.post(cl, "/akun/verifikasi/unggah", {"kind": "foto"}, {}).status_code, 400)
        # ktp slot smuggled into foto kind is ignored (cannot overwrite KTP)
        r = self.post(cl, "/akun/verifikasi/unggah", {"kind": "foto"},
                      {"ktp_redacted": ("k.jpg", t2.img_bytes(), "image/jpeg")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.post(cl, "/akun/verifikasi/unggah", {"kind": "foto"}, img).status_code, 303)
        self.assertEqual(self.post(cl, "/akun/verifikasi/unggah", {"kind": "bogus"}, img).status_code, 400)
        self.assertEqual(self.post(cl, "/akun/verifikasi/unggah", {"kind": "foto"}, img, csrf=False).status_code, 403)

    def test_reupload_bad_image(self):
        cl, mid = self.member()
        self.reject(mid, "ktp")
        r = self.post(cl, "/akun/verifikasi/unggah", {"kind": "ktp"}, {"ktp_redacted": ("k.jpg", b"zzz", "image/jpeg")})
        self.assertEqual(r.status_code, 400)


class TestIntros(P):
    def setUp(self):
        super().setUp()
        self.a, self.ma = self.member()
        self.b, self.mb = self.member(full_name="Siti Kandidat", domicile="Bandung", intro="Intro kandidat B yang cukup panjang untuk lolos.")
        self.i = self.intro(self.ma, self.mb)

    def test_card_has_fields_but_no_contact(self):
        r = self.a.get("/akun")
        self.assertIn("Bandung", r.text)
        self.assertIn("Intro kandidat B", r.text)
        self.assertIn("Sama-sama suka padel.", r.text)
        self.assertIn(f"/akun/foto/{self.i}", r.text)
        self.assertIn("Tertarik", r.text)
        self.assertNotIn("08123000", r.text)
        self.assertNotIn("62812300", r.text)
        self.assertNotIn("Siti Kandidat", r.text)

    def test_contact_only_when_both_interested(self):
        self.assertEqual(self.post(self.a, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"}).status_code, 303)
        self.assertNotIn("62812300", self.a.get("/akun").text)
        self.assertNotIn("62812300", self.b.get("/akun").text)
        self.assertEqual(self.post(self.b, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"}).status_code, 303)
        self.assertIn("6281230000002", self.a.get("/akun").text)
        self.assertIn("6281230000001", self.b.get("/akun").text)

    def test_decline_hides_from_other_side_and_is_final(self):
        self.post(self.a, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"})
        self.post(self.b, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tidak"})
        ra = self.a.get("/akun").text
        self.assertNotIn("62812300", ra)
        self.assertNotIn("menolak", ra.lower())            # decliner's choice is not revealed
        self.assertIn("Menunggu", ra)
        r = self.post(self.b, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT status FROM introductions").fetchone()[0], "declined")

    def test_idor_third_member_and_csrf(self):
        c, mc = self.member()
        r = self.post(c, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(c.get(f"/akun/foto/{self.i}").status_code, 404)
        self.assertNotIn("Bandung", c.get("/akun").text)
        self.assertEqual(self.post(self.a, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"}, csrf=False).status_code, 403)
        self.assertEqual(self.post(self.a, "/akun/perkenalan/%d/respon" % self.i, {"choice": "x"}).status_code, 400)
        self.assertIsNone(self.w.execute("SELECT resp_a FROM introductions").fetchone()[0])

    def test_photo_route_serves_only_counterpart_face(self):
        r = self.a.get(f"/akun/foto/{self.i}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(r.headers["cache-control"], "no-store")
        face_b = self.w.execute("SELECT stored_path FROM member_files WHERE member_id=? AND kind='photo_face'", (self.mb,)).fetchone()[0]
        self.assertEqual(r.content, rb(os.path.join(self.priv, face_b)))
        # no route addresses body/KTP/own files; anon refused
        self.assertEqual(self.c.get(f"/akun/foto/{self.i}").status_code, 303)
        self.assertEqual(self.a.get("/akun/foto/9999").status_code, 404)

    def test_photo_withheld_for_declined_pair(self):
        self.w.execute("UPDATE introductions SET status='declined'")
        self.assertEqual(self.a.get(f"/akun/foto/{self.i}").status_code, 404)

    def test_symmetry_member_b_side(self):
        r = self.post(self.b, "/akun/perkenalan/%d/respon" % self.i, {"choice": "tertarik"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.w.execute("SELECT resp_a,resp_b FROM introductions").fetchone()[:], (None, "tertarik"))


class TestPlatinumSlot(P):
    def setUp(self):
        super().setUp()
        self.a, self.ma = self.member(program="5")
        self.b, self.mb = self.member()
        self.i = self.intro(self.ma, self.mb, status="mutual", ra="tertarik", rb="tertarik")
        self.s1 = self.w.execute("INSERT INTO dinner_slots(introduction_id,starts_at,venue) VALUES(?,?,?)",
                                 (self.i, "2027-01-10 19:00", "Resto Dummy")).lastrowid
        self.s2 = self.w.execute("INSERT INTO dinner_slots(introduction_id,starts_at,venue) VALUES(?,?,?)",
                                 (self.i, "2027-01-11 19:00", "Resto Dummy 2")).lastrowid

    def test_only_platinum_sees_and_picks_once(self):
        self.assertIn("Resto Dummy", self.a.get("/akun").text)
        self.assertNotIn("Resto Dummy", self.b.get("/akun").text)
        r = self.post(self.b, f"/akun/perkenalan/{self.i}/slot", {"slot_id": self.s1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.post(self.a, f"/akun/perkenalan/{self.i}/slot", {"slot_id": self.s1}).status_code, 303)
        self.assertEqual(self.post(self.a, f"/akun/perkenalan/{self.i}/slot", {"slot_id": self.s2}).status_code, 400)
        self.assertEqual(self.w.execute("SELECT picked_by FROM dinner_slots WHERE id=?", (self.s1,)).fetchone()[0], self.ma)

    def test_slot_of_other_intro_rejected(self):
        c, mc = self.member(program="5")
        j = self.intro(mc, self.mb, status="mutual", ra="tertarik", rb="tertarik")
        r = self.post(c, f"/akun/perkenalan/{j}/slot", {"slot_id": self.s1})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self.w.execute("SELECT picked_by FROM dinner_slots WHERE id=?", (self.s1,)).fetchone()[0])
        r = self.post(c, f"/akun/perkenalan/{self.i}/slot", {"slot_id": self.s1})
        self.assertEqual(r.status_code, 404)


class TestFeedback(P):
    def setUp(self):
        super().setUp()
        self.a, self.ma = self.member()
        self.b, self.mb = self.member()
        self.i = self.intro(self.ma, self.mb, status="mutual", ra="tertarik", rb="tertarik")

    def test_submit_once_private(self):
        p = f"/akun/perkenalan/{self.i}/feedback"
        for bad in ("0", "6", "x", ""):
            self.assertEqual(self.post(self.a, p, {"rating": bad, "note": "n"}).status_code, 400, bad)
        self.assertEqual(self.post(self.a, p, {"rating": "4", "note": "Orangnya ramah rahasia-A"}).status_code, 303)
        self.assertEqual(self.post(self.a, p, {"rating": "5", "note": "lagi"}).status_code, 400)
        self.assertEqual(self.post(self.a, p, {"rating": "5", "note": "x" * 1001}).status_code, 400)
        self.assertNotIn("rahasia-A", self.b.get("/akun").text)
        row = self.w.execute("SELECT member_id,rating FROM feedback").fetchone()
        self.assertEqual((row[0], row[1]), (self.ma, 4))

    def test_requires_mutual_and_party_and_csrf(self):
        p = f"/akun/perkenalan/{self.i}/feedback"
        self.w.execute("UPDATE introductions SET status='proposed'")
        self.assertEqual(self.post(self.a, p, {"rating": "4"}).status_code, 400)
        self.w.execute("UPDATE introductions SET status='mutual'")
        c, _ = self.member()
        self.assertEqual(self.post(c, p, {"rating": "4"}).status_code, 404)
        self.assertEqual(self.post(self.a, p, {"rating": "4"}, csrf=False).status_code, 403)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)


class TestProfile(P):
    def form(self, **over):
        d = dict(domicile="Jakarta Barat", marga="", education="S1", height_cm="170",
                 intro="Saya orang dummy yang suka padel dan kopi, mencari teman hidup.",
                 pref_age_min="25", pref_age_max="35", pref_religion="Islam", pref_domicile="Jabodetabek",
                 pref_notes="", publish_consent="ya_blur", ethnicity="Local Indonesia", religion="Islam",
                 marital_status="Single")
        d.update(over)
        return d

    def test_form_prefilled(self):
        cl, _ = self.member()
        self.assertIn("Jakarta Barat", cl.get("/akun/profil").text)

    def test_noncore_edit_keeps_review_flag_off(self):
        cl, mid = self.member()
        r = self.post(cl, "/akun/profil", self.form(domicile="Surabaya", height_cm="175"), back="/akun/profil")
        self.assertEqual(r.status_code, 303)
        m = self.w.execute("SELECT domicile,height_cm,perlu_review FROM members WHERE id=?", (mid,)).fetchone()
        self.assertEqual(tuple(m), ("Surabaya", 175, 0))

    def test_core_change_sets_perlu_review(self):
        cl, mid = self.member()
        self.assertEqual(self.post(cl, "/akun/profil", self.form(religion="Kristen"), back="/akun/profil").status_code, 303)
        self.assertEqual(self.w.execute("SELECT perlu_review FROM members WHERE id=?", (mid,)).fetchone()[0], 1)
        cl2, m2 = self.member()
        self.post(cl2, "/akun/profil", self.form(marital_status="Menikah/Tunangan/Pacaran"), back="/akun/profil")
        self.assertEqual(self.w.execute("SELECT perlu_review FROM members WHERE id=?", (m2,)).fetchone()[0], 1)

    def test_invalid_rejected_nothing_written(self):
        cl, mid = self.member()
        for bad in (dict(religion="Alien"), dict(height_cm="9"), dict(intro="pendek"), dict(pref_age_min="40"),
                    dict(domicile=""), dict(publish_consent="x")):
            r = self.post(cl, "/akun/profil", self.form(**bad), back="/akun/profil")
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.w.execute("SELECT religion,height_cm FROM members WHERE id=?", (mid,)).fetchone()[:], ("Islam", 170))

    def test_identity_fields_not_editable(self):
        cl, mid = self.member()
        before = tuple(self.w.execute("SELECT full_name,birth_date,phone,email,gender FROM members WHERE id=?", (mid,)).fetchone())
        self.post(cl, "/akun/profil", self.form(full_name="Hacker", birth_date="2000-01-01", phone="628111",
                                                email="h@x.test", gender="Perempuan"), back="/akun/profil")
        after = tuple(self.w.execute("SELECT full_name,birth_date,phone,email,gender FROM members WHERE id=?", (mid,)).fetchone())
        self.assertEqual(before, after)

    def test_csrf_and_own_row_only(self):
        a, ma = self.member()
        b, mb = self.member()
        self.assertEqual(self.post(a, "/akun/profil", self.form(domicile="X"), csrf=False).status_code, 403)
        self.post(a, "/akun/profil", self.form(domicile="Solo", id=str(mb), member_id=str(mb)), back="/akun/profil")
        self.assertEqual(self.w.execute("SELECT domicile FROM members WHERE id=?", (mb,)).fetchone()[0], "Jakarta Barat")


class TestDelete(P):
    def test_request_needs_password_confirm_and_is_idempotent(self):
        cl, mid = self.member()
        pw = "dummy-Passw0rd-123"
        self.assertEqual(self.post(cl, "/akun/hapus", {"password": "salah-salah-salah", "confirm": "on"}).status_code, 400)
        self.assertEqual(self.post(cl, "/akun/hapus", {"password": pw}).status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM deletion_requests").fetchone()[0], 0)
        self.assertEqual(self.post(cl, "/akun/hapus", {"password": pw, "confirm": "on"}, csrf=False).status_code, 403)
        self.assertEqual(self.post(cl, "/akun/hapus", {"password": pw, "confirm": "on"}).status_code, 303)
        self.post(cl, "/akun/hapus", {"password": pw, "confirm": "on"})
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM deletion_requests WHERE member_id=? AND status='open'", (mid,)).fetchone()[0], 1)
        self.assertIn("Owner", cl.get("/akun").text)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members WHERE id=?", (mid,)).fetchone()[0], 1)  # request only

    def test_audit_has_no_pii(self):
        cl, mid = self.member()
        self.post(cl, "/akun/hapus", {"password": "dummy-Passw0rd-123", "confirm": "on"})
        self.post(cl, "/akun/profil", TestProfile.form(self, religion="Kristen"), back="/akun/profil")
        rows = " ".join(r[0] + " " + r[1] for r in self.w.execute("SELECT action,detail FROM audit_log"))
        for pii in ("08123000", "62812300", "example.test", "Budi", "Jakarta", ".enc", ".jpg"):
            self.assertNotIn(pii, rows)
        self.assertIn("member_delete_request", rows)


class TestMigrationV3(unittest.TestCase):
    def test_v2_to_v3_preserves_rows_and_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.db")
            c = db.connect(p)
            db.init_db(c)
            c.execute("PRAGMA user_version=2")
            for t in ("dinner_slots", "feedback", "deletion_requests"):
                c.execute(f"DROP TABLE {t}")
            c.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES('ACK-0001','Dummy','1990-01-01','t')")
            c.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at) VALUES(1,1,'pending','t')")
            c.execute("INSERT INTO payments(invoice_code,subscription_id,amount_idr,status,created_at) VALUES('INV-2601-0001',1,5,'pending','t')")
            c.close()
            self.assertEqual(db.dryrun(p), db.SCHEMA_VERSION)
            c = db.connect(p)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)    # dryrun did not touch source
            db.init_db(c)
            db.init_db(c)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(c.execute("SELECT amount_idr,unique_code FROM payments").fetchone()[:], (5, None))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)
            cols = {r[1] for r in c.execute("PRAGMA table_info(introductions)")}
            self.assertTrue({"resp_a", "resp_b", "match_reason"} <= cols)
            c.close()


class TestFileSafety(P):
    """Fault injection: DB failure must keep old files + valid rows; cleanup failure is retained and retried."""

    def setUp(self):
        super().setUp()
        self.cl, self.mid = self.member()
        self.reject(self.mid, "ktp")
        self.old = self.w.execute("SELECT * FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (self.mid,)).fetchone()
        self.up = {"ktp_redacted": ("k.jpg", t2.img_bytes(color=(1, 2, 3)), "image/jpeg")}

    def reject(self, mid, kind, why="x"):
        self.w.execute("INSERT INTO verifications(member_id,kind,status,note,created_at) VALUES(?,?,'rejected',?,?)",
                       (mid, kind, why, db.now_iso()))

    def files(self):
        return set(os.listdir(self.priv))

    def pending(self):
        return self.w.execute("SELECT * FROM pending_file_cleanup").fetchall()

    def fail_audit(self):
        """Make the last statement of the upload tx (audit insert) raise -> whole tx rolls back."""
        self.w.execute("CREATE TRIGGER boom BEFORE INSERT ON audit_log WHEN NEW.action='member_reupload' "
                       "BEGIN SELECT RAISE(ABORT,'boom'); END")

    def test_db_failure_keeps_old_file_and_row(self):
        before = self.files()
        self.fail_audit()
        r = self.post(self.cl, "/akun/verifikasi/unggah", {"kind": "ktp"}, self.up)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.files(), before)                       # old kept, new removed
        rows = self.w.execute("SELECT * FROM member_files WHERE member_id=? AND kind='ktp_redacted'", (self.mid,)).fetchall()
        self.assertEqual([x["id"] for x in rows], [self.old["id"]])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.w.execute("SELECT status FROM verifications WHERE member_id=? AND kind='ktp'", (self.mid,)).fetchone()[0], "rejected")

    def test_commit_failure_keeps_old_file(self):
        before = self.files()
        real = db.tx

        from contextlib import contextmanager

        @contextmanager
        def bad_tx(conn):
            with real(conn):
                yield conn
                raise sqlite3.OperationalError("commit failed")
        with mock.patch.object(db, "tx", bad_tx):
            self.assertEqual(self.post(self.cl, "/akun/verifikasi/unggah", {"kind": "ktp"}, self.up).status_code, 500)
        self.assertEqual(self.files(), before)
        self.assertTrue(self.w.execute("SELECT 1 FROM member_files WHERE id=?", (self.old["id"],)).fetchone())
        self.assertEqual(self.pending(), [])

    def test_cleanup_failure_retained_then_retried(self):
        real = os.unlink
        oldp = self.old["stored_path"]

        def flaky(p, *a, **k):
            if os.path.basename(p) == oldp:
                raise PermissionError("nope")
            return real(p, *a, **k)
        with mock.patch("os.unlink", flaky):
            self.assertEqual(self.post(self.cl, "/akun/verifikasi/unggah", {"kind": "ktp"}, self.up).status_code, 303)
        p = self.pending()
        self.assertEqual([(x["stored_path"], x["member_id"], x["attempts"]) for x in p], [(oldp, self.mid, 1)])
        self.assertIn(oldp, self.files())                            # retained, not lost, not untracked
        c = self.w
        self.assertEqual(portal.reconcile_pending(c, self.priv), 1)  # explicit retry
        self.assertNotIn(oldp, self.files())
        self.assertEqual(self.pending(), [])
        self.assertEqual(portal.reconcile_pending(c, self.priv), 0)

    def test_reconcile_never_deletes_referenced_file(self):
        self.w.execute("INSERT INTO pending_file_cleanup(member_id,stored_path,created_at) VALUES(?,?,'t')",
                       (self.mid, self.old["stored_path"]))
        portal.reconcile_pending(self.w, self.priv)
        self.assertIn(self.old["stored_path"], self.files())
        self.assertEqual(self.pending(), [])

    def test_second_photo_write_failure_cleans_first(self):
        self.reject(self.mid, "foto")
        before = self.files()
        real = register._write_private
        calls = []

        def second_fails(directory, data, ext, name=None):
            calls.append(name)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(directory, data, ext, name)
        up = {"photo_body": ("b.jpg", t2.img_bytes(), "image/jpeg"), "photo_face": ("f.jpg", t2.img_bytes(), "image/jpeg")}
        with mock.patch.object(register, "_write_private", second_fails):
            self.assertEqual(self.post(self.cl, "/akun/verifikasi/unggah", {"kind": "foto"}, up).status_code, 500)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.files(), before)                       # first new file removed, olds kept
        self.assertEqual(self.pending(), [])

    def test_partial_write_is_removed_even_if_unlink_fails_then_retried(self):
        real_w = os.fdopen

        class Boom:
            def __init__(s, fh): s.fh = fh
            def __enter__(s): return s
            def __exit__(s, *a): s.fh.close()
            def write(s, d): s.fh.write(d[:3]); raise OSError("short write")
        before = self.files()
        with mock.patch("os.fdopen", lambda fd, m: Boom(real_w(fd, m))):
            self.assertEqual(self.post(self.cl, "/akun/verifikasi/unggah", {"kind": "ktp"}, self.up).status_code, 500)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.pending(), [])

    def test_proof_replace_db_failure_keeps_old_proof(self):
        self.priced_program()
        cl, mid = self.member(program="2")
        up = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}
        self.assertEqual(self.post(cl, "/akun/bayar/unggah", files_=up).status_code, 303)
        old = self.pay(mid)["proof_file_id"]
        oldp = self.w.execute("SELECT stored_path FROM member_files WHERE id=?", (old,)).fetchone()[0]
        self.w.execute("CREATE TRIGGER boom2 BEFORE INSERT ON audit_log WHEN NEW.action='member_payment_proof' "
                       "BEGIN SELECT RAISE(ABORT,'boom'); END")
        before = self.files()
        self.assertEqual(self.post(cl, "/akun/bayar/unggah", files_=up).status_code, 500)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.pay(mid)["proof_file_id"], old)
        self.assertIn(oldp, self.files())


class TestTruthLabels(P):
    def test_timeline_factual_per_stage(self):
        f = portal.timeline
        st = lambda *a, **k: [s[1] for s in f(*a, **k)]
        self.assertEqual(st(False, False, False, True, invoiced=False), ["done", "na", "current", "todo", "done"])
        self.assertEqual(st(False, False, False, False, invoiced=True), ["done", "current", "todo", "todo", "todo"])

    def test_null_invoice_and_mutual_page(self):
        a, ma = self.member()
        b, mb = self.member()
        self.intro(ma, mb, status="mutual", ra="tertarik", rb="tertarik")
        r = a.get("/akun")
        self.assertIn("belum ada tagihan", r.text)
        self.assertRegex(r.text, r"Perkenalan<small>selesai")

    def test_occupation_and_proposed_photo(self):
        a, ma = self.member()
        b, mb = self.member(occupation="Dokter Gigi")
        i = self.intro(ma, mb)                      # proposed: photo + job shown, contact not
        r = a.get("/akun")
        self.assertIn("Dokter Gigi", r.text)
        self.assertIn(f"/akun/foto/{i}", r.text)
        self.assertEqual(a.get(f"/akun/foto/{i}").status_code, 200)
        self.assertNotIn("08123000", r.text)
        self.assertNotIn("wa.me/62812300", r.text)
        self.assertNotIn("Instagram", r.text.split("Perkenalan</h2>")[1])
        self.assertEqual(b.get(f"/akun/foto/{i}").status_code, 200)
        c, _ = self.member()
        self.assertEqual(c.get(f"/akun/foto/{i}").status_code, 404)    # non-party


if __name__ == "__main__":
    unittest.main()
