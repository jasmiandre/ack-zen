"""Pilot intake gate tests. Synthetic data only; every DB/private dir is a TemporaryDirectory, .env is mocked."""
import contextlib, hashlib, os, re, shutil, sqlite3, tempfile, unittest
from unittest import mock

from fastapi.testclient import TestClient

import seed
import test_stage2 as t2
import test_stage4_staff as t4
import test_stage5_event as t5
import test_stage5_guest as g
from app import create_app

SECRET = "k" * 32
PNG = {"proof": ("b.png", t2.img_bytes("PNG"), "image/png")}
KTP = {"ktp_redacted": ("k.jpg", t2.img_bytes(color=(1, 2, 3)), "image/jpeg")}


class Isolated(unittest.TestCase):
    """No live .env / PILOT_INTAKE_OPEN leaks in; originals restored by patch.dict / patch (LIFO)."""
    env_file = {}

    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start(); self.addCleanup(p.stop)
        os.environ.pop("PILOT_INTAKE_OPEN", None)
        self.env_file = dict(self.env_file)
        p = mock.patch.object(seed, "read_env", lambda path: dict(self.env_file))
        p.start(); self.addCleanup(p.stop)


def snap(root, dbp):
    """All table contents + every non-DB file under root (recursive, sha256) + directory names."""
    con = sqlite3.connect(dbp)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        tables = {n: sorted(repr(tuple(r)) for r in con.execute(f'SELECT * FROM "{n}"')) for n in names}
    finally:
        con.close()
    files, dirs = {}, []
    for d, ds, fs in os.walk(root):
        dirs += [os.path.relpath(os.path.join(d, x), root) for x in ds]
        for f in fs:
            if os.path.basename(dbp) in f:
                continue   # db + -wal/-shm are covered by the table dump
            with open(os.path.join(d, f), "rb") as fh:
                files[os.path.relpath(os.path.join(d, f), root)] = hashlib.sha256(fh.read()).hexdigest()
    return tables, files, sorted(dirs)


class ClosedGate(Isolated):
    def run_app(self, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)                       # runs LAST (LIFO) ...
        c = TestClient(create_app(db_path=os.path.join(d.name, "g.db"), secret_key=SECRET, fernet_key="", **kw))
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)    # ... after lifespan shutdown closed the DB
        return c, d.name

    def probe_open(self, **kw):
        c, _ = self.run_app(**kw)
        return c.post("/daftar/submit", data={}).status_code != 503

    def test_default_closed_and_explicit(self):
        self.assertFalse(self.probe_open())
        self.assertFalse(self.probe_open(pilot_open=False))
        self.assertTrue(self.probe_open(pilot_open=True))

    def test_blocked_response_shape_and_reads_open(self):
        c, _ = self.run_app()
        r = c.post("/daftar/submit", data={"csrf_token": "bad"})
        self.assertEqual((r.status_code, r.headers.get("retry-after")), (503, "86400"))
        self.assertEqual(c.get("/").status_code, 200)

    def test_closed_keeps_login(self):
        c, _ = self.run_app()
        self.assertNotEqual(c.post("/login", data={}).status_code, 503)

    def test_env_strict(self):
        for v, want in (("true", True), (" true ", True), ("TRUE", False), ("True", False), ("1", False), ("yes", False),
                        ("false", False), ("", False)):
            with self.subTest(os_env=v):
                with mock.patch.dict(os.environ, {"PILOT_INTAKE_OPEN": v}):
                    self.assertEqual(self.probe_open(), want)

    def test_env_file_source_and_precedence(self):
        self.env_file = {"PILOT_INTAKE_OPEN": "true"}      # approved fixture instead of the live .env
        self.assertTrue(self.probe_open())
        self.assertFalse(self.probe_open(pilot_open=False))   # explicit arg beats .env
        os.environ["PILOT_INTAKE_OPEN"] = "false"             # non-empty process env wins over .env
        self.assertFalse(self.probe_open())
        os.environ["PILOT_INTAKE_OPEN"] = ""                  # empty falls through to .env
        self.assertTrue(self.probe_open())
        self.env_file = {"PILOT_INTAKE_OPEN": "TRUE"}
        os.environ.pop("PILOT_INTAKE_OPEN")
        self.assertFalse(self.probe_open())

    def test_env_restored(self):
        os.environ["PILOT_INTAKE_OPEN"] = "sentinel"
        keep = dict(os.environ)
        self.test_env_strict.__func__(self)  # mutates and must leave the env as found
        self.assertEqual(dict(os.environ), keep)


class Gate(Isolated, t4.S):
    """Real fixtures (members, staff sessions, event, payments, proofs) built on an OPEN app, then the same
    sessions replayed against a CLOSED app on the same DB. A control replay on a copy proves each request
    would have mutated state if the gate were not there."""

    def setUp(self):
        Isolated.setUp(self)
        t4.S.setUp(self)
        self.addCleanup(self.close_all)   # runs before base tmp.cleanup: DB closed first
        self.clients = {}   # close_all may run even if setUp fails early
        self.fin = self.staff("fin", "finance")
        self.evs = self.staff("evt", "event")
        self.ver = self.staff("ver", "verifier")
        self.assertEqual(self.spost(self.evs, "/staff/event/baru", t5.ev(capacity="6", quota_m="3", quota_f="3")).status_code, 303)
        self.eid = self.w.execute("SELECT id FROM events WHERE slug='dinner-demo'").fetchone()[0]
        self.w.execute("UPDATE programs SET price_idr=1500000, duration_days=30 WHERE id=2")
        self.sel_cl, _, self.sel_rid = self.selected("Laki-laki")
        self.paid_cl, _, self.paid_rid = self.selected("Perempuan")
        self.assertEqual(self.paid_cl.post("/akun/event/bukti", data={"rid": str(self.paid_rid), "csrf_token": self.tok(self.paid_cl)},
                                           files=PNG).status_code, 303)
        self.reg_cl, _ = self.member(gender="Perempuan")
        self.pay_cl, _, self.pid = self.paying(proof=True)
        self.ver_cl, vmid = self.member()
        self.w.execute("INSERT INTO verifications(member_id,kind,status,note,created_at) VALUES(?,'ktp','rejected','x','t')", (vmid,))
        self.clients = {"fin": self.fin, "evs": self.evs, "ver": self.ver, "sel": self.sel_cl, "paid": self.paid_cl,
                        "reg": self.reg_cl, "pay": self.pay_cl, "vercl": self.ver_cl}

    def close_all(self):
        for c in list(self.clients.values()):
            c.__exit__()
        self.clients = {}

    def selected(self, gender):
        cl, mid = self.member(gender=gender)
        r = cl.post("/event/dinner-demo/daftar", data={"csrf_token": self.tok(cl, "/event/dinner-demo")})
        self.assertIn(r.status_code, (200, 303), r.text)
        rid = self.w.execute("SELECT id FROM event_regs WHERE member_id=?", (mid,)).fetchone()[0]
        self.assertEqual(self.spost(self.evs, f"/staff/event/{self.eid}/pendaftar/{rid}/pilih").status_code, 200)
        return cl, mid, rid

    # world = (clients by name, root dir, db path); open/closed app over the same DB, or over a copy.
    def world(self, open_, copy=False):
        root, dbp = self.tmp.name, self.dbp
        if copy:
            td = tempfile.TemporaryDirectory()
            self.addCleanup(td.cleanup)
            root, dbp = td.name, os.path.join(td.name, "t.db")
            dst = sqlite3.connect(dbp)
            self.w.backup(dst)
            dst.close()
            if os.path.isdir(self.priv):
                shutil.copytree(self.priv, os.path.join(root, "private"))
        app = create_app(pilot_open=open_, db_path=dbp, secret_key=t2.SECRET, cookie_secure=False, fernet_key=t2.FKEY.decode())
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        cls = {}
        for name in list(self.clients) + ["anon", "anon2"]:
            c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
            c.__enter__()
            stack.callback(c.__exit__)   # client first (closes app DB) before tmp cleanup
            if name in self.clients:
                c.cookies.update(self.clients[name].cookies)
            cls[name] = c
        return cls, root, dbp

    def cases(self):
        """(name, path, prep(cls)->(client, request kwargs)); prep does the token GETs, only the POST is measured."""
        rx = lambda c, p: re.search(r'name="csrf_token" value="([^"]+)"', c.get(p).text).group(1)

        def daftar(w):
            c = w["anon"]
            return c, dict(data=t2.fields(phone="081277770001", email="new1@example.test"), files=t2.files(),
                           headers={"X-CSRF-Token": rx(c, "/daftar")})

        def tamu(w):
            c = w["anon2"]
            return c, dict(data=g.gf(), files=g.gfiles(), headers={"X-CSRF-Token": rx(c, "/event/dinner-demo/tamu")})

        def evdaftar(w):
            return w["reg"], dict(data={"csrf_token": rx(w["reg"], "/event/dinner-demo")})

        def evbukti(w):
            return w["sel"], dict(data={"rid": str(self.sel_rid), "csrf_token": rx(w["sel"], "/akun")}, files=PNG)

        def verif(w):
            return w["vercl"], dict(data={"kind": "ktp", "csrf_token": rx(w["vercl"], "/akun")}, files=KTP)

        def fin(w):
            return w["fin"], dict(data={"csrf_token": rx(w["fin"], "/dashboard")})

        return [("/daftar/submit", "/daftar/submit", daftar), ("/event/tamu/submit", "/event/tamu/submit", tamu),
                ("/event/{slug}/daftar", "/event/dinner-demo/daftar", evdaftar),
                ("/akun/event/bukti", "/akun/event/bukti", evbukti),
                ("/akun/verifikasi/unggah", "/akun/verifikasi/unggah", verif),
                ("/keuangan/event/{id}/setuju", f"/keuangan/event/{self.paid_rid}/setuju", fin)]

    def attempt(self, w, root, dbp, prep, path):
        c, kw = prep(w)
        before = snap(root, dbp)
        r = c.post(path, follow_redirects=True, **kw)
        return r, before, snap(root, dbp)

    def test_fixture_not_vacuous(self):
        tables, files, _ = snap(self.tmp.name, self.dbp)
        for t in ("members", "payments", "event_regs", "member_files", "users_staff", "sessions"):
            self.assertTrue(tables[t], t)
        self.assertTrue(files)   # encrypted private uploads exist

    def test_control_open_copy_mutates_for_every_blocked_route(self):
        for name, path, prep in self.cases():
            with self.subTest(route=name):
                w, root, dbp = self.world(True, copy=True)
                r, before, after = self.attempt(w, root, dbp, prep, path)
                self.assertNotEqual(r.status_code, 503)
                self.assertNotEqual(before, after, "request had no effect even when open: fixture too weak")

    def test_closed_authenticated_valid_csrf_no_effects(self):
        for name, path, prep in self.cases():
            with self.subTest(route=name):
                w, root, dbp = self.world(False)
                r, before, after = self.attempt(w, root, dbp, prep, path)
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.headers["retry-after"], "86400")
                self.assertEqual(before, after)

    def test_closed_path_variants_never_reach_handler(self):
        def variants(p):
            return [p + "/", "/" + p, p + "?x=1", "/%" + format(ord(p[1]), "02X") + p[2:]]
        for name, path, prep in self.cases():
            for v in variants(path):
                with self.subTest(route=name, path=v):
                    w, root, dbp = self.world(False)
                    r, before, after = self.attempt(w, root, dbp, prep, v)
                    self.assertIn(r.status_code, (503, 404, 405), r.status_code)
                    self.assertEqual(before, after)

    def test_closed_keeps_nonmoney_staff_and_member_flows(self):
        w, root, dbp = self.world(False)
        self.assertEqual(w["fin"].get("/keuangan").status_code, 200)
        c = w["fin"]
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/keuangan").text).group(1)
        r = c.post(f"/keuangan/{self.pid}/tolak", data={"csrf_token": tok, "reason": "buram"}, follow_redirects=True)
        self.assertIn(r.status_code, (200, 409))   # 409 = gate+CSRF ok but no proof attached yet; never 503
        self.assertEqual(self.w.execute("SELECT status FROM payments WHERE id=?", (self.pid,)).fetchone()[0], "rejected")
        self.assertEqual(w["sel"].get("/akun").status_code, 200)

    def test_open_roles_csrf_exact(self):
        """Open gate leaves auth intact: exact outcomes, never a blanket 'any of 303/401/403'."""
        w, root, dbp = self.world(True)
        for path, fixture in ((f"/keuangan/{self.pid}/setuju", "payments"), (f"/keuangan/event/{self.paid_rid}/setuju", "event_regs")):
            with self.subTest(path=path):
                state = snap(root, dbp)
                tok = self.stok(w["fin"])
                for who, data, code, loc in (("anon", {}, 303, "/login"),
                                             ("sel", {}, 303, "/login"),                          # member session is not staff
                                             ("fin", {}, 403, None),                              # valid session, no csrf
                                             ("fin", {"csrf_token": "bad"}, 403, None),           # valid session, bad csrf
                                             ("evs", {"csrf_token": self.stok(w["evs"])}, 403, None),  # wrong role, valid csrf
                                             ("ver", {"csrf_token": self.stok(w["ver"])}, 403, None)):
                    r = w[who].post(path, data=data)
                    self.assertEqual(r.status_code, code, (who, data))
                    self.assertEqual(r.headers.get("location"), loc, who)
                self.assertEqual(snap(root, dbp), state, "rejected requests must not mutate")
        r = w["fin"].post(f"/keuangan/{self.pid}/setuju", data={"csrf_token": self.stok(w["fin"])})   # valid proof is attached
        self.assertEqual((r.status_code, r.headers.get("location")), (303, "/keuangan"))
        self.assertEqual(self.w.execute("SELECT status FROM payments WHERE id=?", (self.pid,)).fetchone()[0], "approved")
        r = w["fin"].post(f"/keuangan/event/{self.paid_rid}/setuju", data={"csrf_token": self.stok(w["fin"])})
        self.assertEqual((r.status_code, r.headers.get("location")), (200, None))   # fin_queue re-render, not a redirect
        self.assertEqual(self.w.execute("SELECT status FROM event_regs WHERE id=?", (self.paid_rid,)).fetchone()[0], "paid")


if __name__ == "__main__":
    unittest.main()
