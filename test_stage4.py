"""ACK Stage 4 schema tests: v4 -> v5 additive migration. Synthetic data only."""
import os
import shutil
import sqlite3
import tempfile
import unittest

import db

NEW = {"subscriptions": {"starts_at", "ends_at"},
       "members": {"pipeline", "internal_notes", "next_followup_at"},
       "verifications": {"approved_at", "checklist"}}


def rb(p):
    with open(p, "rb") as f:
        return f.read()


def cols(c, t):
    return {r[1] for r in c.execute(f"PRAGMA table_info({t})")}


class V5(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "t.db")
        c = db.connect(self.path)
        db.init_db(c)
        # rewind to a genuine v4 shape: drop v5 columns/table, seed rows
        for t, names in NEW.items():
            for n in names:
                c.execute(f"ALTER TABLE {t} DROP COLUMN {n}")
        c.execute("DROP TABLE app_settings")
        c.execute("PRAGMA user_version=4")
        self.code = db.add_member(c, "Dummy A", "1990-01-01")
        mid = c.execute("SELECT id FROM members").fetchone()[0]
        pid = c.execute("SELECT id FROM programs LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at) VALUES(?,?,'pending','t')", (mid, pid))
        c.execute("INSERT INTO verifications(member_id,kind,created_at) VALUES(?,'ktp','t')", (mid,))
        c.execute("INSERT INTO pending_file_cleanup(member_id,stored_path,created_at) VALUES(?,'x/y','t')", (mid,))
        self.c = c
        self.addCleanup(lambda: self.c.close())   # self.c is rebound by test_dryrun; close whichever is current

    def snap(self):
        return {t: [tuple(r) for r in self.c.execute(f"SELECT * FROM {t} ORDER BY id")]
                for t in ("members", "subscriptions", "verifications", "pending_file_cleanup")}

    def test_version(self):
        self.assertEqual(db.SCHEMA_VERSION, 9)

    def test_v4_to_v5_preserves_rows(self):
        before = self.snap()
        for t, names in NEW.items():
            self.assertFalse(names & cols(self.c, t))
        db.init_db(self.c)
        for t, names in NEW.items():
            self.assertTrue(names <= cols(self.c, t))
        self.assertEqual(self.c.execute("PRAGMA user_version").fetchone()[0], 9)
        after = self.snap()
        for t, rows in before.items():     # old values intact; new cols NULL
            n = len(rows[0])
            self.assertEqual([r[:n] for r in after[t]], rows)
        self.assertIsNone(self.c.execute("SELECT starts_at FROM subscriptions").fetchone()[0])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM pending_file_cleanup").fetchone()[0], 1)

    def test_retention_default_30(self):
        db.init_db(self.c)
        self.assertEqual(self.c.execute("SELECT value FROM app_settings WHERE key='ktp_retention_days'").fetchone()[0], "30")

    def test_idempotent_twice_keeps_config(self):
        db.init_db(self.c)
        self.c.execute("UPDATE app_settings SET value='45' WHERE key='ktp_retention_days'")
        snap = self.snap()
        self.c.execute("PRAGMA user_version=4")    # force rerun
        db.init_db(self.c)
        db.init_db(self.c)
        self.assertEqual(self.snap(), snap)
        self.assertEqual(self.c.execute("SELECT value FROM app_settings WHERE key='ktp_retention_days'").fetchone()[0], "45")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0], 1)

    def test_existing_checks_survive(self):
        db.init_db(self.c)
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE subscriptions SET status='bogus'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE members SET birth_date='1990/01/01'")

    def test_dryrun_leaves_original(self):
        self.c.close()
        raw = rb(self.path)
        self.assertEqual(db.dryrun(self.path), 9)
        c = sqlite3.connect(self.path)
        self.addCleanup(c.close)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertFalse(NEW["members"] & cols(c, "members"))
        self.assertEqual(rb(self.path), raw)
        self.c = db.connect(self.path)


if __name__ == "__main__":
    unittest.main()
