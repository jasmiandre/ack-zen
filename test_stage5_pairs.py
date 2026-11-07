"""ACK Stage 5 final chunk: pairing + post-event mutual picks. Synthetic data only."""
import json
import re
import sqlite3
import threading
import unittest

import db
import event_pairs
import pairing
import test_stage4_staff as t4


class Algo(unittest.TestCase):
    def test_selfcheck(self):
        pairing._selfcheck()

    def test_odd_and_bounds(self):
        P = [(i, "Laki-laki" if i % 2 else "Perempuan", pairing.LEVELS[i % 3]) for i in range(1, 8)]
        for rd in pairing.suggest(P, 4):
            ids = [x for p in rd for x in p if x]
            self.assertEqual(sorted(ids), list(range(1, 8)))
        with self.assertRaises(ValueError):
            pairing.suggest([(1, "x", None), (1, "x", None)], 1)


class Mig(unittest.TestCase):
    def test_v10_idempotent_preserves(self):
        c = db.connect(":memory:")
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO events(title,created_at) VALUES('a','t')")
        c.execute("PRAGMA user_version=9")
        db.migrate(c)
        db.migrate(c)
        c.execute("PRAGMA user_version=9")
        db.migrate(c)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        for t in ("event_pairings", "event_picks", "event_connections"):
            c.execute(f"SELECT COUNT(*) FROM {t}")
        c.close()


class P(t4.S):
    def setUp(self):
        super().setUp()
        self.ev = self.staff("evt", "event")
        self.fin = self.staff("fin", "finance")
        self.w.execute("INSERT INTO events(title,slug,starts_at,ends_at,capacity,quota_m,quota_f,price_idr,active,created_at,extra_fields) "
                       "VALUES('Dinner','dinner-demo','2020-01-01T10:00','2020-01-01T12:00',9,9,9,1,1,'t','[]')")
        self.eid = 1
        self.n_intro = self.cnt("introductions")

    def cnt(self, t):
        return self.w.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    def attendee(self, gender, status="checked_in", level="Pemula"):
        cl, mid = self.member(gender=gender)
        self.w.execute("INSERT INTO event_regs(event_id,member_id,status,created_at,answers) VALUES(1,?,?,'t',?)",
                       (mid, status, json.dumps({"level": level})))
        code = self.w.execute("SELECT code FROM members WHERE id=?", (mid,)).fetchone()[0]
        return cl, mid, code

    def pick(self, cl, code, eid=1, csrf=True):
        d = {"event_id": str(eid), "code": code}
        if csrf:
            d["csrf_token"] = self.tok(cl)
        return cl.post("/akun/event/pilihan", data=d)

    # ---- staff ----
    def test_staff_rbac_csrf_preview_save(self):
        a = self.attendee("Laki-laki")
        b = self.attendee("Perempuan")
        self.attendee("Perempuan", status="paid")   # not checked in: excluded
        p = f"/staff/event/{self.eid}/pasangan"
        self.assertEqual(self.fin.get(p).status_code, 403)
        self.assertEqual(self.fin.post(p + "/pratinjau", data={"rounds": "1", "csrf_token": self.stok(self.fin)}).status_code, 403)
        self.assertEqual(self.ev.post(p + "/pratinjau", data={"rounds": "1"}).status_code, 403)
        self.assertEqual(self.spost(self.ev, p + "/pratinjau", {"rounds": "9"}).status_code, 400)
        r = self.spost(self.ev, p + "/pratinjau", {"rounds": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(a[2], r.text)
        self.assertEqual(self.cnt("event_pairings"), 0)   # preview writes nothing
        good = {"r1_a_0": str(a[1]), "r1_b_0": str(b[1])}
        self.assertEqual(self.spost(self.ev, p + "/simpan", good).status_code, 200)
        self.assertEqual(self.spost(self.ev, p + "/simpan", good).status_code, 200)   # idempotent
        self.assertEqual(self.cnt("event_pairings"), 1)
        for bad in ({"r1_a_0": str(a[1]), "r1_b_0": str(a[1])},                      # self
                    {"r1_a_0": str(a[1]), "r1_b_0": "999"},                           # not eligible
                    {"r1_a_0": str(a[1]), "r1_b_0": str(b[1]), "r1_a_1": str(b[1])},  # duplicate in round
                    {"r9_a_0": str(a[1])}, {"r1_a_0": "x"}):
            self.assertEqual(self.spost(self.ev, p + "/simpan", bad).status_code, 400, bad)
        self.assertEqual(self.cnt("event_pairings"), 1)   # failed edits did not wipe the saved set
        self.assertEqual(self.spost(self.ev, "/staff/event/99/pasangan/simpan", good).status_code, 404)

    # ---- picks ----
    def test_mutual_creates_one_connection_contact_only_after_both(self):
        a, b, c = self.attendee("Laki-laki"), self.attendee("Perempuan"), self.attendee("Perempuan")
        r = self.pick(a[0], b[2])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cnt("event_connections"), 0)
        phone_b = self.w.execute("SELECT phone FROM members WHERE id=?", (b[1],)).fetchone()[0]
        self.assertNotIn(phone_b, r.text)
        self.assertNotIn(phone_b, b[0].get("/akun/event/1/pilihan").text)   # b hasn't chosen yet; one-way pick shows nothing
        r = self.pick(b[0], a[2])
        self.assertEqual(self.cnt("event_connections"), 1)
        phone_a = self.w.execute("SELECT phone FROM members WHERE id=?", (a[1],)).fetchone()[0]
        self.assertIn(phone_a, r.text)
        self.assertIn(phone_b, a[0].get("/akun/event/1/pilihan").text)
        self.assertNotIn(phone_a, c[0].get("/akun/event/1/pilihan").text)   # third party sees nothing
        self.assertNotIn(phone_b, c[0].get("/akun/event/1/pilihan").text)
        self.pick(b[0], a[2])
        self.pick(a[0], b[2])
        self.assertEqual((self.cnt("event_connections"), self.cnt("event_picks")), (1, 2))   # repeat = no duplicate
        self.assertEqual(self.cnt("introductions"), self.n_intro)   # Stage 4 program intros untouched / quota not counted

    def test_rejects(self):
        a, b = self.attendee("Laki-laki"), self.attendee("Perempuan")
        o = self.attendee("Perempuan", status="paid")
        self.assertEqual(self.pick(a[0], a[2]).status_code, 400)         # self
        self.assertEqual(self.pick(a[0], o[2]).status_code, 400)         # not checked in
        self.assertEqual(self.pick(a[0], "NOPE").status_code, 400)
        self.assertEqual(self.pick(o[0], a[2]).status_code, 404)         # chooser not checked in
        self.assertEqual(self.pick(a[0], b[2], csrf=False).status_code, 403)
        self.w.execute("INSERT INTO events(title,slug,ends_at,created_at) VALUES('E2','e2','2020-01-01T12:00','t')")
        self.assertEqual(a[0].post("/akun/event/pilihan", data={"event_id": "2", "code": b[2], "csrf_token": self.tok(a[0])}).status_code, 404)  # cross-event
        self.assertEqual(self.cnt("event_picks"), 0)

    def test_before_end_and_no_ends_at(self):
        a, b = self.attendee("Laki-laki"), self.attendee("Perempuan")
        for end in ("2999-01-01T12:00", None):
            self.w.execute("UPDATE events SET ends_at=? WHERE id=1", (end,))
            self.assertEqual(self.pick(a[0], b[2]).status_code, 400)
        self.assertEqual(self.cnt("event_picks"), 0)

    def test_expired_program_does_not_block(self):
        a, b = self.attendee("Laki-laki"), self.attendee("Perempuan")
        self.w.execute("UPDATE subscriptions SET ends_at='2000-01-01T00:00:00Z'")
        self.pick(a[0], b[2])
        self.pick(b[0], a[2])
        self.assertEqual(self.cnt("event_connections"), 1)

    def test_concurrent_double_mutual_one_connection(self):
        a, b = self.attendee("Laki-laki"), self.attendee("Perempuan")
        jobs = [(x[0], {"event_id": "1", "code": y[2], "csrf_token": self.tok(x[0])}) for x, y in ((a, b), (b, a)) * 3]
        rs = []
        ts = [threading.Thread(target=lambda c=c, d=d: rs.append(c.post("/akun/event/pilihan", data=d).status_code)) for c, d in jobs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(rs, [200] * 6)
        self.assertEqual(self.cnt("event_connections"), 1)

    def test_no_apply_response(self):
        import inspect
        self.assertNotIn("apply_response", inspect.getsource(event_pairs))


if __name__ == "__main__":
    unittest.main()
