"""Stage 4 matching/introduction/quota/dinner tests. Synthetic data only."""
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

import db
import matching
import test_stage4_staff as ts

NOW = db.now_iso()
FUT = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class M(ts.S):
    def setUp(self):
        super().setUp()
        self.mm = self.staff("mm", "matchmaker")

    def mem(self, gender="Laki-laki", quota=3, prog=2, ends=FUT, **over):
        _, mid = self.member(gender=gender, **over)
        self.w.execute("UPDATE members SET status='verified' WHERE id=?", (mid,))
        self.w.execute("UPDATE programs SET intro_quota=? WHERE id=?", (quota, prog))
        self.w.execute("DELETE FROM subscriptions WHERE member_id=?", (mid,))
        self.w.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at,starts_at,ends_at) VALUES(?,?,'active','t','2020-01-01T00:00:00Z',?)",
                       (mid, prog, ends))
        return mid

    def prop(self, a, b, cl=None):
        cl = cl or self.mm
        return self.spost(cl, f"/pasangan/{a}/usul", {"candidate": str(b)})

    def n(self):
        return self.w.execute("SELECT COUNT(*) FROM introductions").fetchone()[0]

    def respond(self, iid, col, choice="tertarik"):
        with db.tx(self.w):
            return matching.apply_response(self.w, iid, col, choice)


class TestPure(unittest.TestCase):
    A = dict(id=1, gender="Laki-laki", birth_date="1995-01-01", religion="Islam", domicile="Jakarta", ethnicity="Batak", education="S1",
             marga="  Sitorus ", pref_age_min=25, pref_age_max=35, pref_religion="Islam", pref_domicile="Jakarta")
    B = dict(A, id=2, gender="Perempuan", marga="x")

    def test_marga_normalised_exact(self):
        self.assertTrue(matching.same_marga({"marga": "SITORUS"}, {"marga": " sitorus  "}))
        self.assertFalse(matching.same_marga({"marga": ""}, {"marga": ""}))
        self.assertFalse(matching.same_marga({"marga": "Sitorus"}, {"marga": "Sitorusx"}))

    def test_unknown_pref_no_constraint_and_deterministic(self):
        c = dict(self.B, pref_age_min=None, pref_age_max=None, pref_religion=None, pref_domicile=None)
        s1 = matching.score_pair(self.A, c, date(2026, 1, 1))
        self.assertEqual(s1, matching.score_pair(self.A, c, date(2026, 1, 1)))
        self.assertTrue(matching.hard_ok(self.A, c))
        self.assertTrue(any("belum diisi" in r for r in s1[1]))

    def test_top10_order_and_gender(self):
        cs = [dict(self.B, id=i, religion="Islam" if i % 2 else "Hindu") for i in range(2, 30)] + [dict(self.A, id=99)]
        t = matching.top_matches(self.A, cs, date(2026, 1, 1))
        self.assertEqual(len(t), 10)
        self.assertTrue(all(x["m"]["gender"] == "Perempuan" for x in t))
        self.assertEqual([x["m"]["id"] for x in t], sorted([x["m"]["id"] for x in t], key=lambda i: (-next(y["score"] for y in t if y["m"]["id"] == i), i)))


class TestFlow(M):
    def test_roles_csrf_idor(self):
        a, b = self.mem(), self.mem("Perempuan")
        self.assertEqual(self.staff("fi", "finance").get(f"/pasangan/{a}").status_code, 403)
        self.assertEqual(self.staff("ve", "verifier").post(f"/pasangan/{a}/usul", data={"candidate": b}).status_code, 403)
        self.assertEqual(self.c.get(f"/pasangan/{a}").headers["location"], "/login")
        self.assertEqual(self.spost(self.mm, f"/pasangan/{a}/usul", {"candidate": str(b)}, csrf=False).status_code, 403)
        self.assertEqual(self.n(), 0)
        self.assertEqual(self.mm.get(f"/pasangan/{a}").status_code, 200)
        self.assertEqual(self.mm.get("/pasangan/99999").status_code, 404)
        mc, _ = self.member()   # a member session cannot reach staff pages
        self.assertIn(mc.get(f"/pasangan/{a}").status_code, (303, 403))

    def test_page_lists_candidate_with_reasons_no_auto_proposal(self):
        a, b = self.mem(), self.mem("Perempuan")
        t = self.mm.get(f"/pasangan/{a}").text
        self.assertIn("agama", t); self.assertEqual(self.n(), 0)
        self.assertEqual(self.prop(a, b).status_code, 200)
        self.assertEqual(self.n(), 1)

    def test_duplicate_either_direction_and_decline_permanent(self):
        a, b = self.mem(), self.mem("Perempuan")
        self.prop(a, b); self.prop(a, b); self.prop(b, a)
        self.assertEqual(self.n(), 1)
        self.respond(1, "resp_a", "tidak")      # one-sided decline
        self.assertEqual(self.prop(b, a).status_code, 400)
        self.assertEqual(self.n(), 1)

    def test_hard_eligibility(self):
        a = self.mem(marga="Sitorus")
        for kw, why in ((dict(gender="Laki-laki"), "same gender"), (dict(gender="Perempuan", marga=" SITORUS "), "marga"),
                        (dict(gender="Perempuan", marital_status="Menikah/Tunangan/Pacaran"), "married"),
                        (dict(gender="Perempuan", marital_status="Belum Resmi Bercerai"), "not divorced")):
            b = self.mem(**kw)
            self.assertEqual(self.prop(a, b).status_code, 400, why)
        b = self.mem("Perempuan"); self.w.execute("UPDATE members SET status='pending' WHERE id=?", (b,))
        self.assertEqual(self.prop(a, b).status_code, 400)
        self.w.execute("UPDATE members SET status='verified', pipeline='Matched' WHERE id=?", (b,))
        self.assertEqual(self.prop(a, b).status_code, 400)
        self.w.execute("UPDATE members SET pipeline='Nonaktif' WHERE id=?", (b,))
        self.assertEqual(self.prop(a, b).status_code, 400)
        self.assertEqual(self.n(), 0)
        self.w.execute("UPDATE members SET pipeline='Aktif' WHERE id=?", (b,))
        self.assertEqual(self.prop(a, b).status_code, 200); self.assertEqual(self.n(), 1)

    def test_quota_null_zero_expired(self):
        a, b = self.mem(quota=None), self.mem("Perempuan", quota=None)
        self.assertEqual(self.prop(a, b).status_code, 400)
        self.w.execute("UPDATE programs SET intro_quota=0 WHERE id=2")
        self.assertEqual(self.prop(a, b).status_code, 400)
        self.w.execute("UPDATE programs SET intro_quota=5 WHERE id=2")
        self.assertEqual(self.prop(a, b).status_code, 200)
        self.w.execute("UPDATE subscriptions SET ends_at=?", (PAST,))
        c = self.mem("Perempuan", ends=PAST)
        self.assertEqual(self.prop(a, c).status_code, 400)
        self.assertEqual(self.n(), 1)
        self.assertEqual(self.w.execute("SELECT status FROM introductions").fetchone()[0], "proposed")   # existing stays
        self.assertIn("A-", self.mm.get("/perkenalan").text.replace("ACK-", "A-"))

    def test_last_quota_reserved_not_double_assigned(self):
        a, b, c = self.mem(quota=1), self.mem("Perempuan", quota=1), self.mem("Perempuan", quota=1)
        self.assertEqual(self.prop(a, b).status_code, 200)
        self.assertEqual(self.prop(a, c).status_code, 400)       # a's only unit is reserved by the pending proposal
        self.assertEqual(self.n(), 1)

    def test_last_quota_concurrent(self):
        a = self.mem(quota=1); cs = [self.mem("Perempuan", quota=1) for _ in range(4)]
        cls = [(self.staff(f"mx{i}", "matchmaker"), c) for i, c in enumerate(cs)]
        toks = [self.stok(cl) for cl, _ in cls]
        out = []
        def go(cl, c, t):
            out.append(cl.post(f"/pasangan/{a}/usul", data={"candidate": str(c), "csrf_token": t}).status_code)
        th = [threading.Thread(target=go, args=(cl, c, t)) for (cl, c), t in zip(cls, toks)]
        [t.start() for t in th]; [t.join() for t in th]
        self.assertEqual(sorted(out), [200, 400, 400, 400]); self.assertEqual(self.n(), 1)

    def test_mutual_consumes_once_declined_no_refund(self):
        a, b, c = self.mem(quota=2), self.mem("Perempuan", quota=2), self.mem("Perempuan", quota=2)
        self.prop(a, b); self.prop(a, c)
        self.assertEqual(self.respond(1, "resp_a"), "proposed")
        self.assertEqual(self.w.execute("SELECT quota_consumed FROM introductions WHERE id=1").fetchone()[0], 0)
        self.assertEqual(self.respond(1, "resp_b"), "mutual")
        self.assertIsNone(self.respond(1, "resp_b"))                      # no second transition
        self.assertEqual(self.w.execute("SELECT SUM(quota_consumed) FROM introductions").fetchone()[0], 1)
        self.respond(2, "resp_a"); self.respond(2, "resp_b", "tidak")    # declined: nothing consumed
        self.assertEqual(self.w.execute("SELECT SUM(quota_consumed) FROM introductions").fetchone()[0], 1)
        for m in (a, b):
            row = self.w.execute(matching._BASE + "WHERE m.id=?", (m,)).fetchone()
            self.assertEqual(matching.capacity(self.w, row, NOW)[0], 1 if m == a else 1)
        self.assertEqual(matching.capacity(self.w, self.w.execute(matching._BASE + "WHERE m.id=?", (c,)).fetchone(), NOW)[0], 2)

    def test_legacy_stage3_intro_not_charged(self):
        a, b = self.mem(quota=1), self.mem("Perempuan", quota=1)
        iid = self.w.execute("INSERT INTO introductions(member_a,member_b,status,created_at) VALUES(?,?,'proposed',?)", (a, b, NOW)).lastrowid
        self.respond(iid, "resp_a"); self.assertEqual(self.respond(iid, "resp_b"), "mutual")
        self.assertEqual(self.w.execute("SELECT quota_consumed FROM introductions").fetchone()[0], 0)

    def test_workflow_transitions_dinner_platinum_only(self):
        a, b = self.mem(quota=2), self.mem("Perempuan", quota=2)
        self.prop(a, b)
        act = lambda **d: self.spost(self.mm, "/perkenalan/1/aksi", d)
        self.assertEqual(act(action="jadwal").status_code, 400)               # not mutual
        self.assertEqual(act(action="resp_a", choice="tertarik").status_code, 200)
        self.assertEqual(act(action="resp_b", choice="tertarik").status_code, 200)
        fut = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(act(action="slot", starts_at=fut, venue="Cafe Dummy").status_code, 400)   # no Platinum
        self.w.execute("UPDATE programs SET name='ACK Platinum Uji' WHERE id=2")
        for bad in ("2001-01-01T10:00", "garbage", ""):
            self.assertEqual(act(action="slot", starts_at=bad, venue="x").status_code, 400)
        self.assertEqual(act(action="slot", starts_at=fut, venue="Cafe Dummy").status_code, 200)
        self.assertEqual(act(action="hasil", result="berhasil").status_code, 400)  # not scheduled yet
        self.assertEqual(act(action="jadwal").status_code, 400)                     # no slot picked by member yet
        self.w.execute("UPDATE dinner_slots SET picked_by=?", (a,))
        self.assertEqual(act(action="jadwal").status_code, 200)
        self.assertEqual(act(action="hasil", result="bogus").status_code, 400)
        self.assertEqual(act(action="hasil", result="berhasil").status_code, 200)
        self.assertEqual(act(action="hasil", result="tidak_lanjut").status_code, 400)   # final
        r = self.w.execute("SELECT outcome,quota_consumed FROM introductions").fetchone()
        self.assertEqual(tuple(r), ("berhasil", 1))

    def test_contact_not_leaked_before_mutual(self):
        a, b = self.mem(), self.mem("Perempuan")
        self.prop(a, b)
        ph = self.w.execute("SELECT phone FROM members WHERE id=?", (b,)).fetchone()[0]
        cl = self.mm.get("/perkenalan").text + self.mm.get(f"/pasangan/{a}").text
        self.assertNotIn(ph, cl)


class TestMigration(unittest.TestCase):
    def test_v6_additive_and_idempotent(self):
        c = db.connect(":memory:"); db.init_db(c); db.migrate(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(introductions)")}
        self.assertTrue({"quota_tracked", "quota_consumed", "mutual_at", "outcome"} <= cols)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        c.close()


if __name__ == "__main__":
    unittest.main()
