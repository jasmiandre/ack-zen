"""ACK Stage 2 tests (stdlib unittest + TestClient): member registration. Synthetic images, dummy data only."""
import asyncio
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from io import BytesIO
from unittest import mock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

import auth
import db
import register
from app import BodyLimit, create_app

HERE = os.path.dirname(os.path.abspath(__file__))
FKEY = Fernet.generate_key()
SECRET = "test-secret-key-0123456789abcdef"


def img_bytes(fmt="JPEG", size=(80, 60), exif=False, color=(200, 30, 30)):
    im = Image.new("RGB", size, color)
    out = BytesIO()
    kw = {}
    if exif:
        e = Image.Exif()
        e[0x010F] = "SyntheticCam"   # Make
        e[0x0112] = 6                # Orientation: rotate 90 on display
        kw["exif"] = e
    im.save(out, fmt, **kw)
    return out.getvalue()


def fields(**over):
    d = dict(full_name="Budi Dummy", phone="0812-3456-7890", email="Budi.Dummy@Example.test",
             password="dummy-Passw0rd-123", password2="dummy-Passw0rd-123", gender="Laki-laki",
             birth_date="1995-05-17", domicile="Jakarta Barat", ethnicity="Local Indonesia", marga="",
             religion="Islam", marital_status="Single", education="S1", height_cm="170",
             intro="Saya orang dummy yang suka padel dan kopi, mencari teman hidup.",
             pref_age_min="25", pref_age_max="35", pref_religion="Islam", pref_domicile="Jabodetabek",
             pref_notes="", publish_consent="ya_blur", source="Instagram",
             ktp_nik_ok="on", ktp_addr_ok="on", ktp_sign_ok="on", program_id="1",
             consent_terms="on", consent_privacy="on", consent_honest="on")
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


def files(**over):
    f = {"ktp_redacted": ("ktp-redacted.jpg", img_bytes(), "image/jpeg"),
         "photo_body": ("badan.png", img_bytes("PNG", (60, 120)), "image/png"),
         "photo_face": ("wajah.webp", img_bytes("WEBP"), "image/webp")}
    f.update(over)
    return {k: v for k, v in f.items() if v is not None}


class Base(unittest.TestCase):
    secure = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbp = os.path.join(self.tmp.name, "t.db")
        self.app = create_app(db_path=self.dbp, secret_key=SECRET, cookie_secure=self.secure,
                              fernet_key=FKEY.decode())
        self.c = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__)
        self.addCleanup(self.tmp.cleanup)
        self.priv = os.path.join(self.tmp.name, "private")
        p = mock.patch.object(register, "SUBMIT_LIMIT", 1000)   # rate-limit test restores the real bound
        p.start()
        self.addCleanup(p.stop)
        self.q = sqlite3.connect(self.dbp)
        self.q.row_factory = sqlite3.Row
        self.addCleanup(self.q.close)

    def csrf(self, client=None):
        r = (client or self.c).get("/daftar")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertTrue(m, r.status_code)
        return m.group(1)

    def submit(self, f=None, fl=None, client=None, csrf=None):
        c = client or self.c
        tok = csrf if csrf is not None else self.csrf(c)
        return c.post("/daftar/submit", data=f if f is not None else fields(),
                      files=fl if fl is not None else files(), headers={"X-CSRF-Token": tok})

    def n(self, table):
        return self.q.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestRules(unittest.TestCase):
    def test_phone_normalised(self):
        for raw in ("0812-3456-7890", "+62 812 3456 7890", "6281234567890", "(0812) 3456 7890"):
            self.assertEqual(register.normalize_phone(raw), "6281234567890", raw)
        for bad in ("", "12345", "0212345678", "abc", "081234", "+1 415 555 0100"):
            self.assertIsNone(register.normalize_phone(bad), bad)

    def test_dob_full_and_18_hard(self):
        today = date(2026, 11, 7)
        ok = lambda s: register.validate(fields(birth_date=s), today)[1].get("birth_date")
        self.assertIsNone(ok("2008-11-07"))                    # exactly 18 today
        self.assertTrue(ok("2008-11-08"))                      # one day short
        self.assertTrue(ok("1995"))                            # bare year
        self.assertTrue(ok("2030-01-01"))                      # future
        self.assertTrue(ok("1995-02-30"))

    def test_no_override_columns(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(members)")}
        self.assertFalse([c for c in cols if "overrid" in c.lower() or "bypass" in c.lower()])
        self.assertIn("birth_date", cols)

    def test_review_flag(self):
        for st, flag in (("Single", 0), ("Cerai Mati (tanpa anak)", 0), ("Belum Resmi Bercerai", 1),
                         ("Menikah/Tunangan/Pacaran", 1)):
            clean, err, _ = register.validate(fields(marital_status=st), date(2026, 11, 7))
            self.assertEqual(err, {}, st)
            self.assertEqual(clean["perlu_review"], flag, st)


class TestImages(unittest.TestCase):
    def test_exif_stripped_orientation_applied_jpeg_out(self):
        raw = img_bytes(exif=True, size=(80, 60))
        self.assertIn(b"SyntheticCam", raw)
        out = register.clean_image(raw)
        im = Image.open(BytesIO(out))
        self.assertEqual(im.format, "JPEG")
        self.assertEqual(len(im.getexif()), 0)
        self.assertNotIn(b"SyntheticCam", out)
        self.assertEqual(im.size, (60, 80))

    def test_resized(self):
        im = Image.open(BytesIO(register.clean_image(img_bytes(size=(4000, 1000)))))
        self.assertEqual(max(im.size), register.MAX_SIDE)

    def test_png_alpha_and_webp(self):
        a = BytesIO()
        Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(a, "PNG")
        self.assertEqual(Image.open(BytesIO(register.clean_image(a.getvalue()))).mode, "RGB")
        register.clean_image(img_bytes("WEBP"))

    def test_rejects_other_formats_junk_and_oversize(self):
        for bad in (img_bytes("GIF"), img_bytes("BMP"), b"not an image", b"", b"\xff\xd8\xff" + b"x" * 50):
            with self.assertRaises(ValueError):
                register.clean_image(bad)
        with self.assertRaises(ValueError):
            register.clean_image(b"\0" * (register.MAX_IMG + 1))

    def test_decompression_bomb_rejected(self):
        out = BytesIO()
        Image.new("L", (9000, 9000)).save(out, "PNG")   # 81 Mpx, a few KB on the wire
        self.assertLess(len(out.getvalue()), register.MAX_IMG)
        with self.assertRaises(ValueError):
            register.clean_image(out.getvalue())


class TestMigration(unittest.TestCase):
    OLD = """CREATE TABLE members(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE CHECK(code GLOB 'ACK-[0-9][0-9][0-9][0-9]*'),
      full_name TEXT NOT NULL, birth_date TEXT NOT NULL CHECK(birth_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(birth_date)=birth_date),
      status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','verified','rejected')), created_at TEXT NOT NULL);
      CREATE TABLE member_files(id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
      kind TEXT NOT NULL, stored_path TEXT NOT NULL, created_at TEXT NOT NULL);
      INSERT INTO members(code,full_name,birth_date,status,created_at) VALUES('ACK-0001','Lama Dummy','1990-01-01','verified','x');
      INSERT INTO member_files(member_id,kind,stored_path,created_at) VALUES(1,'ktp','old.bin','x');"""

    def test_additive_idempotent_preserves_rows(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "o.db")
            c = db.connect(p)
            c.executescript(self.OLD)
            for _ in range(2):
                db.init_db(c)
            r = c.execute("SELECT * FROM members").fetchall()
            self.assertEqual((len(r), r[0]["full_name"], r[0]["status"], r[0]["perlu_review"]), (1, "Lama Dummy", "verified", 0))
            self.assertEqual(c.execute("SELECT stored_path,encrypted FROM member_files").fetchone()[:], ("old.bin", 0))
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            c.execute("INSERT INTO members(code,full_name,birth_date,status,created_at,email_key,phone) "
                      "VALUES('ACK-0002','A','1990-01-01','pending','x','a@b.c','62811')")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO members(code,full_name,birth_date,status,created_at,email_key,phone) "
                          "VALUES('ACK-0003','B','1990-01-01','pending','x','a@b.c','62822')")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO members(code,full_name,birth_date,status,created_at,email_key,phone) "
                          "VALUES('ACK-0004','B','1990-01-01','pending','x','z@b.c','62811')")
            c.close()

    def test_dryrun_on_copy_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "o.db")
            c = db.connect(p)
            c.executescript(self.OLD)
            c.close()
            self.assertEqual(db.dryrun(p), db.SCHEMA_VERSION)
            c = sqlite3.connect(p)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn("email", {r[1] for r in c.execute("PRAGMA table_info(members)")})
            c.close()
            b = os.path.join(t, "bk.db")
            db.backup(p, b)
            self.assertEqual(sqlite3.connect(b).execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)


class TestRegister(Base):
    def test_get_form_contract(self):
        r = self.c.get("/daftar")
        self.assertEqual(r.status_code, 200)
        h = r.text
        self.assertIn("DATA DUMMY", h)
        self.assertEqual(h.count('class="step"'), 5)
        self.assertEqual(len(re.findall(r'class="prog-card"', h)), 6)
        self.assertIn("Info harga via admin", h)
        self.assertNotIn("Gratis", h)
        self.assertRegex(h, r'<input[^>]*id="ktp-src"(?![^>]*name=)[^>]*>')
        for need in ("ktp_nik_ok", "ktp_addr_ok", "ktp_sign_ok", "consent_terms", "consent_privacy", "consent_honest"):
            self.assertIn(f'name="{need}"', h)
        self.assertNotRegex(h, r"(?i)style=\"")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        c = r.headers["set-cookie"]
        self.assertIn("ack_member=", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("samesite=lax", c.lower())

    def test_js_contract_original_never_sent_no_password_draft(self):
        js = open(os.path.join(HERE, "static", "js", "daftar.js"), encoding="utf-8").read()
        self.assertNotIn("new FormData(form", js)
        self.assertNotIn("new FormData(f)", js)
        self.assertIn("ktp_redacted", js)
        self.assertRegex(js, r"password2?\s*:\s*1")                     # skip-list for the draft
        self.assertIn("localStorage", js)
        self.assertIn("removeItem", js)

    def test_cookie_secure_flag_when_configured(self):
        app = create_app(db_path=os.path.join(self.tmp.name, "s.db"), secret_key=SECRET, cookie_secure=True,
                         fernet_key=FKEY.decode())
        with TestClient(app) as c:
            self.assertIn("Secure", c.get("/daftar").headers["set-cookie"])

    def test_success_full_path(self):
        r = self.submit()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "redirect": "/akun"})
        m = self.q.execute("SELECT * FROM members").fetchone()
        self.assertEqual((m["status"], m["perlu_review"], m["phone"], m["email_key"], m["birth_date"]),
                         ("pending", 0, "6281234567890", "budi.dummy@example.test", "1995-05-17"))
        self.assertTrue(m["password_hash"].startswith("$argon2"))
        self.assertTrue(re.fullmatch(r"ACK-\d{4}", m["code"]))
        sub = self.q.execute("SELECT * FROM subscriptions").fetchone()
        self.assertEqual((sub["status"], sub["member_id"], sub["program_id"]), ("pending", m["id"], 1))
        self.assertEqual(self.n("payments"), 0)     # price NULL: pending subscription, no invoice
        fl = self.q.execute("SELECT * FROM member_files ORDER BY id").fetchall()
        self.assertEqual([x["kind"] for x in fl], ["ktp_redacted", "photo_body", "photo_face"])
        self.assertEqual(sorted(os.listdir(self.priv)), sorted(x["stored_path"] for x in fl))
        f = Fernet(FKEY)
        for x in fl:
            p = os.path.join(self.priv, x["stored_path"])
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertRegex(x["stored_path"], r"^[0-9a-f]{32}\.(enc|jpg)$")
            raw = open(p, "rb").read()
            if x["kind"] == "ktp_redacted":
                self.assertEqual(x["encrypted"], 1)
                self.assertFalse(raw.startswith(b"\xff\xd8"))
                raw = f.decrypt(raw)
            else:
                self.assertEqual(x["encrypted"], 0)
            self.assertTrue(raw.startswith(b"\xff\xd8"))
            self.assertEqual(Image.open(BytesIO(raw)).format, "JPEG")
        self.assertEqual(os.stat(self.priv).st_mode & 0o777, 0o700)
        # lands in the member portal, can log out and back in
        a = self.c.get("/akun")
        self.assertEqual(a.status_code, 200)
        self.assertIn(m["code"], a.text)
        self.assertIn("Budi Dummy", a.text)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', a.text).group(1)
        self.assertEqual(self.c.post("/akun/keluar", data={"csrf_token": tok}).status_code, 303)
        self.assertEqual(self.c.get("/akun").status_code, 303)
        lt = re.search(r'name="csrf_token" value="([^"]+)"', self.c.get("/akun/masuk").text).group(1)
        bad = self.c.post("/akun/masuk", data={"csrf_token": lt, "ident": "budi.dummy@example.test", "password": "nope-nope-nope"})
        self.assertEqual(bad.status_code, 401)
        ok = self.c.post("/akun/masuk", data={"csrf_token": lt, "ident": "08123456789O".replace("O", "0"), "password": "dummy-Passw0rd-123"})
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(self.c.get("/akun").status_code, 200)

    def test_staff_cookie_untouched_and_member_cannot_reach_staff(self):
        self.submit()
        self.assertEqual(self.c.get("/dashboard").status_code, 303)
        self.assertEqual(self.c.get("/owner/staff").status_code, 303)

    def test_member_login_csrf_and_lockout(self):
        self.submit()
        self.c.cookies.clear()
        lt = re.search(r'name="csrf_token" value="([^"]+)"', self.c.get("/akun/masuk").text).group(1)
        self.assertEqual(self.c.post("/akun/masuk", data={"ident": "x@y.z", "password": "p"}).status_code, 403)
        for _ in range(auth.MAX_USER_FAILS):
            self.c.post("/akun/masuk", data={"csrf_token": lt, "ident": "x@y.z", "password": "p"})
        self.assertEqual(self.c.post("/akun/masuk", data={"csrf_token": lt, "ident": "x@y.z", "password": "p"}).status_code, 429)

    def test_priced_program_makes_invoice_with_existing_helper(self):
        self.q.execute("UPDATE programs SET price_idr=150000 WHERE id=2")
        self.q.commit()
        self.assertEqual(self.submit(fields(program_id="2")).status_code, 200)
        p = self.q.execute("SELECT * FROM payments").fetchone()
        self.assertEqual((p["amount_idr"], p["status"]), (150000, "pending"))
        self.assertRegex(p["invoice_code"], r"^INV-\d{4}-0001$")
        self.assertEqual(self.q.execute("SELECT status FROM subscriptions").fetchone()[0], "pending")

    def test_free_program_no_invoice(self):
        self.q.execute("UPDATE programs SET price_idr=0 WHERE id=1")
        self.q.commit()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.n("payments"), 0)

    def test_review_flag_status_stays_pending(self):
        self.assertEqual(self.submit(fields(marital_status="Belum Resmi Bercerai")).status_code, 200)
        m = self.q.execute("SELECT status,perlu_review FROM members").fetchone()
        self.assertEqual((m[0], m[1]), ("pending", 1))

    def test_under_18_rejected_nothing_written(self):
        d = (date.today() - timedelta(days=18 * 365 + 4)).replace(day=1) + timedelta(days=40)   # < 18y
        r = self.submit(fields(birth_date=d.isoformat()))
        self.assertEqual(r.status_code, 400)
        self.assertIn("birth_date", r.json()["errors"])
        self.assertEqual(r.json()["step"], 2)
        self.assertEqual((self.n("members"), self.n("member_files"), self.n("subscriptions")), (0, 0, 0))
        self.assertFalse(os.listdir(self.priv))
        self.assertEqual(self.submit(fields(birth_date="1995")).status_code, 400)

    def test_field_validation(self):
        cases = [dict(full_name=""), dict(phone="12345"), dict(email="nope"), dict(password="short"),
                 dict(password2="different-pass-123"), dict(gender="x"), dict(ethnicity="x"), dict(marital_status="x"),
                 dict(height_cm="20"), dict(intro="terlalu pendek"), dict(pref_age_min="40", pref_age_max="30"),
                 dict(pref_age_min="15"), dict(publish_consent="mungkin"), dict(consent_terms=None),
                 dict(consent_privacy=None), dict(consent_honest=None), dict(ktp_nik_ok=None), dict(ktp_addr_ok=None),
                 dict(ktp_sign_ok=None), dict(program_id="999"), dict(program_id="abc")]
        for c in cases:
            r = self.submit(fields(**c))
            self.assertEqual(r.status_code, 400, c)
            k = next(iter(c))
            self.assertTrue(r.json()["errors"], c)
        self.assertEqual(self.n("members"), 0)

    def test_inactive_program_rejected(self):
        self.q.execute("UPDATE programs SET active=0 WHERE id=1")
        self.q.commit()
        self.assertEqual(self.submit().status_code, 400)

    def test_duplicates_phone_email_no_orphans_no_burned_code(self):
        self.assertEqual(self.submit().status_code, 200)
        before = os.listdir(self.priv)
        c2 = TestClient(self.app, follow_redirects=False)
        for over in (dict(phone="+62 812-3456-7890", email="other@example.test"),
                     dict(phone="0899999999999", email="BUDI.DUMMY@EXAMPLE.TEST")):
            r = self.submit(fields(**over), client=c2)
            self.assertEqual(r.status_code, 409, over)
            self.assertEqual(r.json()["step"], 1)
        self.assertEqual(os.listdir(self.priv), before)
        self.assertEqual(self.n("members"), 1)
        self.assertEqual(self.submit(fields(phone="0877111122223", email="new@example.test"), client=c2).status_code, 200)
        self.assertEqual(sorted(r[0] for r in self.q.execute("SELECT code FROM members")), ["ACK-0001", "ACK-0002"])

    def test_db_constraint_is_the_last_line_of_defence(self):
        self.submit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.q.execute("INSERT INTO members(code,full_name,birth_date,status,created_at,email_key,phone) "
                           "VALUES('ACK-9999','x','1990-01-01','pending','x','budi.dummy@example.test','62800000001')")

    def test_bad_images_rejected_and_cleaned(self):
        bad = {"gif": img_bytes("GIF"), "junk": b"hello", "big": b"\0" * (register.MAX_IMG + 1)}
        for name, data in bad.items():
            for slot in ("ktp_redacted", "photo_body", "photo_face"):
                r = self.submit(fl=files(**{slot: ("a.jpg", data, "image/jpeg")}))
                self.assertEqual(r.status_code, 400, (name, slot))
                self.assertIn(slot, r.json()["errors"])
        r = self.submit(fl=files(photo_face=None))
        self.assertEqual(r.status_code, 400)
        self.assertEqual((self.n("members"), len(os.listdir(self.priv))), (0, 0))

    def test_only_redacted_ktp_field_accepted(self):
        fl = files()
        fl["ktp"] = ("ktp-asli.jpg", img_bytes(), "image/jpeg")
        r = self.submit(fl=fl)
        self.assertEqual(r.status_code, 400)
        fl = files(ktp_redacted=None)
        fl["ktp_original"] = ("ktp-asli.jpg", img_bytes(), "image/jpeg")
        self.assertEqual(self.submit(fl=fl).status_code, 400)
        self.assertEqual(len(os.listdir(self.priv)), 0)

    def test_filename_never_used_for_path(self):
        fl = files(photo_body=("../../../etc/passwd.jpg", img_bytes(), "image/jpeg"))
        self.assertEqual(self.submit(fl=fl).status_code, 200)
        self.assertFalse(any("passwd" in n or ".." in n for n in os.listdir(self.priv)))
        self.assertFalse(any("passwd" in r[0] for r in self.q.execute("SELECT stored_path FROM member_files")))

    def test_csrf_required_on_all_posts(self):
        self.csrf()
        r = self.c.post("/daftar/submit", data=fields(), files=files())
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.submit(csrf="wrong").status_code, 403)
        fresh = TestClient(self.app, follow_redirects=False)       # no cookie at all
        self.assertEqual(fresh.post("/daftar/submit", data=fields(), files=files(), headers={"X-CSRF-Token": "x"}).status_code, 403)
        self.assertEqual(self.c.post("/akun/keluar", data={}).status_code, 403)
        self.assertEqual(self.c.post("/akun/masuk", data={"ident": "a", "password": "b"}).status_code, 403)
        self.assertEqual(self.n("members"), 0)

    def test_rate_limit_bounded(self):
        tok = self.csrf()
        mock.patch.object(register, "SUBMIT_LIMIT", 5).start()
        self.addCleanup(mock.patch.stopall)
        codes = [self.c.post("/daftar/submit", data=fields(full_name=""), files=files(), headers={"X-CSRF-Token": tok}).status_code
                 for _ in range(register.SUBMIT_LIMIT + 3)]
        self.assertEqual(codes[:register.SUBMIT_LIMIT], [400] * register.SUBMIT_LIMIT)
        self.assertEqual(set(codes[register.SUBMIT_LIMIT:]), {429})
        self.assertLessEqual(self.n("reg_attempts"), register.SUBMIT_LIMIT + 1)

    def test_body_limit_counts_received_bytes_not_content_length(self):
        called = []

        async def inner(scope, receive, send):
            called.append(1)
            while True:
                m = await receive()
                if m["type"] != "http.request" or not m.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def run(headers, chunks):
            out, q = [], list(chunks)

            async def receive():
                b = q.pop(0) if q else b""
                return {"type": "http.request", "body": b, "more_body": bool(q)}

            async def send(m):
                out.append(m)
            await BodyLimit(inner, "/daftar/submit", 100)({"type": "http", "method": "POST", "path": "/daftar/submit",
                                                          "headers": headers}, receive, send)
            return out[0]["status"]
        self.assertEqual(asyncio.run(run([(b"content-length", b"5000")], [b"x"])), 413)         # declared too big
        self.assertEqual(asyncio.run(run([], [b"x" * 60, b"x" * 60])), 413)                     # chunked, no header
        self.assertEqual(asyncio.run(run([(b"content-length", b"3")], [b"x" * 60, b"x" * 60])), 413)  # lying header
        self.assertEqual(asyncio.run(run([], [b"x" * 50])), 200)
        self.assertEqual(len(called), 3)  # the declared-too-big one never reached the app

    def test_failure_rolls_back_db_and_removes_files(self):
        self.q.execute("UPDATE programs SET price_idr=1000 WHERE id=1")
        self.q.commit()
        with mock.patch.object(db, "next_invoice_code", side_effect=RuntimeError("boom")):
            r = self.submit()
        self.assertEqual(r.status_code, 500)
        self.assertEqual((self.n("members"), self.n("member_files"), self.n("subscriptions")), (0, 0, 0))
        self.assertEqual(os.listdir(self.priv), [])
        self.assertIsNone(self.q.execute("SELECT value FROM counters WHERE name='member'").fetchone())
        self.assertNotIn("boom", r.text)

    def test_no_pii_in_audit_or_responses(self):
        r = self.submit()
        self.assertEqual(r.status_code, 200)
        blob = " ".join(str(tuple(x)) for x in self.q.execute("SELECT * FROM audit_log"))
        self.assertIn("member_register", blob)
        for pii in ("Budi", "6281234567890", "example.test", "dummy-Passw0rd"):
            self.assertNotIn(pii, blob)

    def test_encryption_requires_key(self):
        app = create_app(db_path=os.path.join(self.tmp.name, "n.db"), secret_key=SECRET, cookie_secure=False, fernet_key="")
        with mock.patch.object(register.os, "environ", {}), TestClient(app, raise_server_exceptions=False) as c:
            if not os.path.exists(os.path.join(HERE, ".env")):
                tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/daftar").text).group(1)
                r = c.post("/daftar/submit", data=fields(), files=files(), headers={"X-CSRF-Token": tok})
                self.assertEqual(r.status_code, 503)


class TestPrivateFiles(Base):
    def staff(self, role):
        u = f"t_{role}_{self.n('users_staff')}"
        cur = self.q.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,?)",
                             (u, u, auth.hash_password("staff-Passw0rd-1"), "x"))
        self.q.execute("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", (cur.lastrowid, role))
        self.q.commit()
        c = TestClient(self.app, follow_redirects=False)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        self.assertEqual(c.post("/login", data={"csrf_token": tok, "username": u, "password": "staff-Passw0rd-1"}).status_code, 303)
        return c

    def test_access_roles_and_audit(self):
        self.submit()
        ids = {r["kind"]: r["id"] for r in self.q.execute("SELECT id,kind FROM member_files")}
        anon = TestClient(self.app, follow_redirects=False)
        self.assertEqual(anon.get(f"/berkas/{ids['ktp_redacted']}").status_code, 303)
        self.assertEqual(self.c.get(f"/berkas/{ids['photo_face']}").status_code, 303)     # the member is not staff
        for role in ("finance", "matchmaker", "event"):
            self.assertEqual(self.staff(role).get(f"/berkas/{ids['photo_face']}").status_code, 403, role)
        for role in ("verifier", "owner"):
            c = self.staff(role)
            for kind, i in ids.items():
                r = c.get(f"/berkas/{i}")
                self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/jpeg"), (role, kind))
                self.assertTrue(r.content.startswith(b"\xff\xd8"))
                self.assertIn("no-store", r.headers["cache-control"])
        self.assertEqual(self.staff("verifier").get("/berkas/9999").status_code, 404)
        views = self.q.execute("SELECT detail FROM audit_log WHERE action='file_view'").fetchall()
        self.assertEqual(len(views), 6)

    def test_not_reachable_via_static_and_names_validated(self):
        self.submit()
        name = os.listdir(self.priv)[0]
        for p in (f"/static/img/{name}", f"/static/{name}", f"/static/css/../../data/private/{name}", f"/data/private/{name}"):
            self.assertEqual(self.c.get(p).status_code, 404, p)
        self.q.execute("UPDATE member_files SET stored_path='../t.db' WHERE id=1")
        self.q.commit()
        self.assertEqual(self.staff("owner").get("/berkas/1").status_code, 404)


class TestLanding(Base):
    def test_ctas_are_real_links(self):
        h = self.c.get("/").text
        self.assertRegex(h, r'<a class="btn btn-primary nav-cta" href="/daftar">Daftar</a>')
        self.assertEqual(len(re.findall(r'href="/daftar"', h)), 3)
        self.assertNotIn("dlg-daftar", h)
        self.assertIn("El Willy Padel", h)

    def test_non_final_legal_still_labelled_draft(self):
        h = self.c.get("/daftar").text
        self.assertIn("DRAF", h)
        self.assertNotRegex(h, r"(?i)disetujui (penasihat|konsultan) hukum")


if __name__ == "__main__":
    unittest.main()
