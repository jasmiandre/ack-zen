"""Stage 4: close()/executescript() on the shared connection wait for another thread's open tx (no lock bypass)."""
import os
import tempfile
import threading
import time
import unittest

import db


class Lifecycle(unittest.TestCase):
    def run_case(self, call):
        with tempfile.TemporaryDirectory() as d:
            c = db.connect(os.path.join(d, "t.db"))
            db.init_db(c)
            started, done, res = threading.Event(), threading.Event(), {}

            def other():
                started.set()
                try:
                    call(c)
                    res["ok"] = True
                except Exception as e:   # noqa: BLE001
                    res["err"] = repr(e)
                done.set()
            with db.tx(c):
                c.execute("INSERT INTO counters(name,value) VALUES('lc',1)")
                t = threading.Thread(target=other); t.start(); started.wait(5)
                time.sleep(0.3)
                self.assertFalse(done.is_set(), "lifecycle call ran inside another thread's tx")
            t.join(10)
            self.assertTrue(res.get("ok"), res)
            return c

    def test_close_waits_for_tx(self):
        c = self.run_case(lambda c: c.close())
        with self.assertRaises(Exception):
            c.execute("SELECT 1")

    def test_executescript_waits_for_tx(self):
        c = self.run_case(lambda c: c.executescript("CREATE TABLE IF NOT EXISTS lc_t(x)"))
        self.assertEqual(c.execute("SELECT value FROM counters WHERE name='lc'").fetchone()[0], 1)   # tx committed intact
        c.close()

    def test_wa_seed_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            c = db.connect(os.path.join(d, "t.db"))
            db.init_db(c); c.execute("UPDATE wa_templates SET body='edited' WHERE key='welcome'"); db.init_db(c)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wa_templates").fetchone()[0], len(db.WA_TEMPLATES))
            self.assertEqual(c.execute("SELECT body FROM wa_templates WHERE key='welcome'").fetchone()[0], "edited")
            c.close()


if __name__ == "__main__":
    unittest.main()
