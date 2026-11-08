import os, tempfile, unittest
from fastapi.testclient import TestClient
from app import create_app
import db

POSTS = ["/daftar/submit", "/event/tamu/submit", "/event/x/daftar", "/akun/event/bukti", "/akun/bayar/unggah",
         "/akun/verifikasi/unggah", "/keuangan/1/setuju", "/keuangan/event/1/setuju"]


class ClosedGate(unittest.TestCase):
    def run_app(self, **kw):
        d = tempfile.mkdtemp()
        c = TestClient(create_app(db_path=os.path.join(d, "g.db"), secret_key="k" * 32, **kw))
        c.__enter__()  # lifespan shutdown closes the DB
        self.addCleanup(c.__exit__, None, None, None)
        return c, d

    def test_closed_blocks_before_effects(self):
        for kw in ({}, {"pilot_open": False}):
            c, d = self.run_app(**kw)
            before = os.listdir(d)
            for p in POSTS:
                r = c.post(p, files={"f": ("a.png", b"x")}, data={"csrf_token": "bad"})
                self.assertEqual(r.status_code, 503, p)
            self.assertEqual(os.listdir(d), before)
            self.assertEqual(c.get("/").status_code, 200)

    def test_closed_keeps_login_and_nonmoney(self):
        c, _ = self.run_app()
        self.assertNotEqual(c.post("/login", data={}).status_code, 503)
        self.assertNotEqual(c.post("/keuangan/1/tolak", data={}).status_code, 503)

    def test_env_strict(self):
        for v, want in (("true", 200), ("TRUE", 503), ("1", 503), ("", 503)):
            os.environ["PILOT_INTAKE_OPEN"] = v
            try:
                c, _ = self.run_app()
                self.assertEqual(c.post("/daftar/submit", data={}).status_code == 503, want == 503, v)
            finally:
                del os.environ["PILOT_INTAKE_OPEN"]

    def test_open_still_defended(self):
        c, _ = self.run_app(pilot_open=True)
        r = c.post("/keuangan/1/setuju", data={}, follow_redirects=False)
        self.assertIn(r.status_code, (303, 401, 403))  # existing auth gate intact when open


if __name__ == "__main__":
    unittest.main()
