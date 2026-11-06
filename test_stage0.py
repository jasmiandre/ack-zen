"""ACK Stage 0 tests (stdlib unittest + FastAPI TestClient). Dummy data only."""
import os
import re
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import date

from fastapi.testclient import TestClient

import auth
import db
import seed
from app import create_app

PW = "dummy-Passw0rd-123"


def csrf_of(client, path="/login"):
    r = client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, f"no csrf on {path}: {r.status_code}"
    return m.group(1)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestAge(unittest.TestCase):
    def test_complete_date_required(self):
        for bad in ["1990", "1990-13-01", "1990-02-30", "", "17/05/1990", "1990-5-7", None]:
            with self.assertRaises(ValueError, msg=str(bad)):
                db.parse_birth_date(bad)
        self.assertEqual(db.parse_birth_date("1990-05-17"), date(1990, 5, 17))

    def test_exact_age(self):
        b = date(2008, 11, 7)
        self.assertEqual(db.age_on(b, date(2026, 11, 6)), 17)
        self.assertEqual(db.age_on(b, date(2026, 11, 7)), 18)
        self.assertFalse(db.is_adult(b, date(2026, 11, 6)))
        self.assertTrue(db.is_adult(b, date(2026, 11, 7)))

    def test_leap_day(self):
        b = date(2008, 2, 29)
        self.assertEqual(db.age_on(b, date(2026, 2, 28)), 17)
        self.assertEqual(db.age_on(b, date(2026, 3, 1)), 18)

    def test_future_birth_rejected(self):
        with self.assertRaises(ValueError):
            db.age_on(date(2030, 1, 1), date(2026, 11, 6))


class TestSchemaAndCodes(DbCase):
    def test_tables(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"users_staff", "members", "member_files", "programs", "subscriptions", "payments",
                "verifications", "introductions", "events", "event_regs", "wa_templates", "audit_log",
                "staff_roles", "sessions", "counters", "login_attempts"}
        self.assertFalse(need - names)

    def test_member_codes_sequential_and_underage(self):
        self.assertEqual(db.add_member(self.conn, "Dummy A", "1990-05-17", date(2026, 11, 6)), "ACK-0001")
        self.assertEqual(db.add_member(self.conn, "Dummy B", "1991-01-01", date(2026, 11, 6)), "ACK-0002")
        with self.assertRaises(ValueError):
            db.add_member(self.conn, "Dummy Minor", "2008-11-07", date(2026, 11, 6))
        # rejected insert must not burn a number
        self.assertEqual(db.add_member(self.conn, "Dummy C", "1992-02-02", date(2026, 11, 6)), "ACK-0003")

    def test_year_only_birth_rejected_by_schema(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES('ACK-9999','x','1990','t')")

    def test_member_code_unique_constraint(self):
        db.add_member(self.conn, "Dummy A", "1990-05-17", date(2026, 11, 6))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES('ACK-0001','x','1990-01-01','t')")

    def test_concurrent_member_codes_unique(self):
        out, errs = [], []

        def work():
            c = db.connect(self.path)
            try:
                out.append(db.add_member(c, "Dummy T", "1990-05-17", date(2026, 11, 6)))
            except Exception as e:  # pragma: no cover
                errs.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=work) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(sorted(out), [f"ACK-{i:04d}" for i in range(1, 21)])

    def test_invoice_codes(self):
        with db.tx(self.conn):
            a = db.next_invoice_code(self.conn, date(2026, 11, 6))
            b = db.next_invoice_code(self.conn, date(2026, 11, 30))
            c = db.next_invoice_code(self.conn, date(2026, 12, 1))
        self.assertEqual((a, b, c), ("INV-2611-0001", "INV-2611-0002", "INV-2612-0001"))

    def test_invoice_rollback_no_gap_and_requires_tx(self):
        with self.assertRaises(RuntimeError):
            db.next_invoice_code(self.conn, date(2026, 11, 6))
        try:
            with db.tx(self.conn):
                db.next_invoice_code(self.conn, date(2026, 11, 6))
                raise KeyError("boom")
        except KeyError:
            pass
        with db.tx(self.conn):
            self.assertEqual(db.next_invoice_code(self.conn, date(2026, 11, 6)), "INV-2611-0001")

    def test_invoice_unique_constraint(self):
        mc = db.add_member(self.conn, "Dummy A", "1990-05-17", date(2026, 11, 6))
        mid = self.conn.execute("SELECT id FROM members WHERE code=?", (mc,)).fetchone()[0]
        pid = self.conn.execute("SELECT id FROM programs LIMIT 1").fetchone()[0]
        self.conn.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at) VALUES(?,?,'pending','t')", (mid, pid))
        sid = self.conn.execute("SELECT id FROM subscriptions").fetchone()[0]
        q = "INSERT INTO payments(invoice_code,subscription_id,status,created_at) VALUES('INV-2611-0001',?,'pending','t')"
        self.conn.execute(q, (sid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(q, (sid,))

    def test_threaded_invoice_unique(self):
        out = []

        def work():
            c = db.connect(self.path)
            with db.tx(c):
                out.append(db.next_invoice_code(c, date(2026, 11, 6)))
            c.close()

        ts = [threading.Thread(target=work) for _ in range(15)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(out)), 15)


class TestPrograms(DbCase):
    def test_seed_six_null_prices(self):
        rows = self.conn.execute("SELECT * FROM programs ORDER BY sort_order").fetchall()
        self.assertEqual([r["name"] for r in rows], [
            "ACK Komunitas", "ACK Bronze", "ACK Silver", "ACK Gold",
            "ACK Platinum / Exclusive", "ACK Private Concierge"])
        self.assertTrue(all(r["price_idr"] is None for r in rows))
        self.assertTrue(all(r["duration_days"] is None and r["intro_quota"] is None for r in rows))
        self.assertEqual([r["proposed"] for r in rows], [0, 0, 0, 1, 0, 1])

    def test_seed_idempotent(self):
        db.init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0], 6)

    def test_price_label_null_is_not_free(self):
        self.assertEqual(db.price_label(None), "Info harga via admin")
        self.assertEqual(db.price_label(0), "Gratis")
        self.assertEqual(db.price_label(150000), "Rp 150.000")

    def test_negative_price_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE programs SET price_idr=-1")


class TestSeed(DbCase):
    def test_env_generated_once_mode600(self):
        p = os.path.join(self.tmp.name, ".env")
        self.assertTrue(seed.init_env(p))
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with open(p) as f:
            before = f.read()
        self.assertFalse(seed.init_env(p))
        with open(p) as f:
            self.assertEqual(f.read(), before)
        env = seed.read_env(p)
        for k in ("SECRET_KEY", "FERNET_KEY", "OWNER_USER", "OWNER_PASS", "PORT", "BANK_INFO", "WA_ADMIN", "COOKIE_SECURE"):
            self.assertIn(k, env)
        self.assertEqual(env["BANK_INFO"], "")
        self.assertEqual(env["WA_ADMIN"], "")
        self.assertEqual(env["COOKIE_SECURE"], "false")
        self.assertEqual(env["PORT"], "8088")

    def test_seed_one_staff_per_role(self):
        cred = os.path.join(self.tmp.name, "seed_credentials.txt")
        n = seed.seed_staff(self.conn, "owner", PW, cred)
        self.assertEqual(n, 5)
        self.assertEqual(stat.S_IMODE(os.stat(cred).st_mode), 0o600)
        for role in db.ROLES:
            c = self.conn.execute("SELECT COUNT(*) FROM staff_roles WHERE role=?", (role,)).fetchone()[0]
            self.assertEqual(c, 1, role)
        with open(cred) as f:
            self.assertNotIn(PW, f.read())  # owner pw lives only in .env
        self.assertEqual(seed.seed_staff(self.conn, "owner", PW, cred), 0)


class AppCase(DbCase):
    def setUp(self):
        super().setUp()
        self.cred = os.path.join(self.tmp.name, "cred.txt")
        seed.seed_staff(self.conn, "owner", PW, self.cred)
        with open(self.cred) as f:
            self.creds = dict(l.strip().split(":", 1) for l in f if ":" in l and not l.startswith("#"))
        self.app = create_app(db_path=self.path, secret_key="test-secret-key-0123456789abcdef", cookie_secure=False)
        self.c = TestClient(self.app, follow_redirects=False)
        self.c.__enter__()  # runs lifespan; exit closes the app's sqlite connection
        self.addCleanup(self.c.__exit__)

    def client(self):
        c = TestClient(self.app, follow_redirects=False)  # shares self.app; lifespan owned by self.c
        self.addCleanup(c.close)
        return c

    def login(self, client, user, pw):
        return client.post("/login", data={"username": user, "password": pw, "csrf_token": csrf_of(client)})

    def as_role(self, role):
        c = self.client()
        u, p = next((u, p) for u, p in self.creds.items() if u.endswith(role))
        self.assertEqual(self.login(c, u, p.strip()).status_code, 303)
        return c


class TestHttpBasics(AppCase):
    def test_health(self):
        r = self.c.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok", "stage": 0, "db": True})

    def test_no_docs_no_static(self):
        for p in ("/docs", "/redoc", "/openapi.json", "/static/x", "/data/ack.db"):
            self.assertEqual(self.c.get(p).status_code, 404, p)

    def test_banner_and_http_warning(self):
        t = self.c.get("/login").text
        self.assertIn("DATA DUMMY", t)
        self.assertIn("HTTP", t)

    def test_security_headers(self):
        h = self.c.get("/login").headers
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("no-store", h["cache-control"])

    def test_secure_cookie_when_configured(self):
        app = create_app(db_path=self.path, secret_key="k" * 32, cookie_secure=True)
        with TestClient(app, follow_redirects=False, base_url="https://testserver") as c:
            r = self.login(c, "owner", PW)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Secure", r.headers["set-cookie"])


class TestAuth(AppCase):
    def test_password_hashed(self):
        h = self.conn.execute("SELECT password_hash FROM users_staff WHERE username='owner'").fetchone()[0]
        self.assertNotIn(PW, h)
        self.assertTrue(auth.verify_password(h, PW))
        self.assertFalse(auth.verify_password(h, "wrong"))

    def test_login_requires_csrf(self):
        r = self.c.post("/login", data={"username": "owner", "password": PW})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/login", data={"username": "owner", "password": PW, "csrf_token": "bad"})
        self.assertEqual(r.status_code, 403)

    def test_bad_credentials_generic(self):
        r1 = self.login(self.c, "owner", "wrong-password-xx")
        r2 = self.login(self.c, "nobody", "wrong-password-xx")
        self.assertEqual((r1.status_code, r2.status_code), (401, 401))
        self.assertNotIn("ack_session", r1.headers.get("set-cookie", ""))

    def test_login_cookie_flags_and_rotation(self):
        self.c.cookies.set("ack_session", "attacker-fixed-value")
        r = self.login(self.c, "owner", PW)
        self.assertEqual(r.status_code, 303)
        sc = r.headers["set-cookie"]
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=lax", sc)
        self.assertNotIn("attacker-fixed-value", sc)
        self.assertEqual(self.c.get("/dashboard").status_code, 200)
        fixed = self.client()
        fixed.cookies.set("ack_session", "attacker-fixed-value")
        self.assertEqual(fixed.get("/dashboard").status_code, 303)

    def test_csrf_rotated_on_login(self):
        before = csrf_of(self.c)
        self.login(self.c, "owner", PW)
        self.assertNotEqual(csrf_of(self.c, "/dashboard"), before)

    def test_logout_csrf_and_server_revocation(self):
        self.login(self.c, "owner", PW)
        token = self.c.cookies.get("ack_session")
        self.assertEqual(self.c.post("/logout", data={}).status_code, 403)
        self.assertEqual(self.c.get("/dashboard").status_code, 200)
        r = self.c.post("/logout", data={"csrf_token": csrf_of(self.c, "/dashboard")})
        self.assertEqual(r.status_code, 303)
        replay = self.client()
        replay.cookies.set("ack_session", token)  # stolen cookie must be dead server-side
        self.assertEqual(replay.get("/dashboard").status_code, 303)

    def test_session_expiry(self):
        self.login(self.c, "owner", PW)
        self.conn.execute("UPDATE sessions SET expires_at=1")
        self.assertEqual(self.c.get("/dashboard").status_code, 303)

    def test_rate_limit_bounded(self):
        for _ in range(auth.MAX_USER_FAILS):
            self.assertEqual(self.login(self.c, "finance_demo_x", "wrong-password-xx").status_code, 401)
        self.assertEqual(self.login(self.c, "finance_demo_x", "wrong-password-xx").status_code, 429)
        # even correct creds on a locked username are refused
        for _ in range(auth.MAX_USER_FAILS):
            self.login(self.c, "owner", "wrong-password-xx")
        self.assertEqual(self.login(self.c, "owner", PW).status_code, 429)
        # old rows are purged, table stays bounded
        self.conn.execute("UPDATE login_attempts SET ts=1")
        self.login(self.c, "other", "wrong-password-xx")
        self.assertLessEqual(self.conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 2)


class TestRoles(AppCase):
    def test_unauthenticated_redirect(self):
        for p in ("/dashboard", "/access/finance", "/owner/staff", "/owner/audit"):
            r = self.c.get(p)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"), p)

    def test_each_role_only_own_area(self):
        for role in ("finance", "verifier", "matchmaker", "event"):
            c = self.as_role(role)
            for other in ("finance", "verifier", "matchmaker", "event"):
                self.assertEqual(c.get(f"/access/{other}").status_code, 200 if other == role else 403, (role, other))
            self.assertEqual(c.get("/owner/staff").status_code, 403, role)
            self.assertEqual(c.get("/owner/audit").status_code, 403, role)
            self.assertEqual(c.post("/owner/staff", data={"csrf_token": csrf_of(c, "/dashboard")}).status_code, 403)

    def test_owner_all_access_and_unknown_role_404(self):
        self.login(self.c, "owner", PW)
        for r in ("finance", "verifier", "matchmaker", "event"):
            self.assertEqual(self.c.get(f"/access/{r}").status_code, 200)
        self.assertEqual(self.c.get("/access/nonsense").status_code, 404)
        self.assertEqual(self.c.get("/owner/staff").status_code, 200)

    def test_owner_creates_multirole_staff_with_csrf(self):
        self.login(self.c, "owner", PW)
        body = {"username": "multi.demo", "display_name": "Multi <script>alert(1)</script>",
                "password": PW, "roles": ["finance", "event"]}
        self.assertEqual(self.c.post("/owner/staff", data=body).status_code, 403)  # no csrf
        body["csrf_token"] = csrf_of(self.c, "/owner/staff")
        self.assertEqual(self.c.post("/owner/staff", data=body).status_code, 303)
        page = self.c.get("/owner/staff").text
        self.assertNotIn("<script>alert(1)", page)  # escaped
        self.assertIn("&lt;script&gt;", page)
        m = self.client()
        self.assertEqual(self.login(m, "multi.demo", PW).status_code, 303)
        self.assertEqual(m.get("/access/finance").status_code, 200)
        self.assertEqual(m.get("/access/event").status_code, 200)
        self.assertEqual(m.get("/access/verifier").status_code, 403)

    def test_owner_create_validation(self):
        self.login(self.c, "owner", PW)
        t = csrf_of(self.c, "/owner/staff")
        base = {"username": "ok.user", "display_name": "x", "password": PW, "roles": ["event"], "csrf_token": t}
        for patch in ({"password": "short"}, {"username": "Bad Name!"}, {"roles": ["god"]}, {"roles": []}, {"username": "owner"}):
            self.assertEqual(self.c.post("/owner/staff", data={**base, **patch}).status_code, 400, patch)

    def test_disable_staff_revokes_sessions(self):
        f = self.as_role("finance")
        self.login(self.c, "owner", PW)
        fid = self.conn.execute("SELECT id FROM users_staff WHERE username LIKE '%finance'").fetchone()[0]
        t = csrf_of(self.c, "/owner/staff")
        self.assertEqual(self.c.post(f"/owner/staff/{fid}/disable", data={}).status_code, 403)
        self.assertEqual(self.c.post(f"/owner/staff/{fid}/disable", data={"csrf_token": t}).status_code, 303)
        self.assertEqual(f.get("/access/finance").status_code, 303)
        self.assertEqual(self.login(self.client(), "demo_finance", "x").status_code, 401)

    def test_password_over_256_rejected_not_truncated(self):
        base = "A" * 256
        self.conn.execute("UPDATE users_staff SET password_hash=? WHERE username='owner'", (auth.hash_password(base),))
        c = self.client()
        self.assertEqual(self.login(c, "owner", base + "suffix").status_code, 401)
        self.assertEqual(self.login(c, "owner", base).status_code, 303)

    def test_run_sh_does_not_source_env(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.sh")) as f:
            src = f.read()
        self.assertNotRegex(src, r"(?m)^\s*(\.|source)\s+\S*\.env")
        self.assertIn("seed.read_env", src)

    def test_app_connection_closed_on_shutdown(self):
        app = create_app(db_path=self.path, secret_key="test-secret-key-0123456789abcdef", cookie_secure=False)
        with TestClient(app) as c:
            self.assertEqual(c.get("/health").status_code, 200)
        self.assertEqual(app.state.conn_closed, True)

    def test_owner_cannot_disable_self(self):
        self.login(self.c, "owner", PW)
        oid = self.conn.execute("SELECT id FROM users_staff WHERE username='owner'").fetchone()[0]
        r = self.c.post(f"/owner/staff/{oid}/disable", data={"csrf_token": csrf_of(self.c, "/owner/staff")})
        self.assertEqual(r.status_code, 400)

    def test_audit_log_has_events_without_secrets(self):
        self.login(self.c, "owner", "wrong-password-xx")
        self.login(self.c, "owner", PW)
        page = self.c.get("/owner/audit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("login_ok", page.text)
        self.assertIn("login_fail", page.text)
        dump = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM audit_log"))
        self.assertNotIn(PW, dump)
        self.assertNotIn("wrong-password-xx", dump)


if __name__ == "__main__":
    unittest.main()
