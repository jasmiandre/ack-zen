"""Stage 4: dashboard aggregates + member list/pipeline. Synthetic data only."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import db
import pipeline
import test_stage2 as t2
import test_stage4_staff as ts

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
iso = pipeline._iso


class PL(ts.S):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(pipeline, "_now", lambda: NOW); p.start(); self.addCleanup(p.stop)
        self.mm, self.own, self.fin = self.staff("mm", "matchmaker"), self.staff("ow", "owner"), self.staff("fi", "finance")

    def mem(self, **over):
        return self.member(**over)[1]

    def set_created(self, mid, when):
        self.w.execute("UPDATE members SET created_at=? WHERE id=?", (iso(when), mid))

    def pp(self, cl, mid, **d):
        return self.spost(cl, f"/anggota/{mid}/pipeline", d)


class TestStats(PL):
    def test_counts_and_boundaries(self):
        a, b, c, d = (self.mem() for _ in range(4))
        self.set_created(a, NOW.replace(hour=0))                  # today, first second
        self.set_created(b, NOW.replace(hour=0) - timedelta(seconds=1))   # yesterday
        self.set_created(c, NOW - timedelta(days=6, hours=12))    # inside 7-day window (today + 6 prior days)
        self.set_created(d, NOW - timedelta(days=7))              # outside
        st = pipeline.stats(self.w, {"matchmaker"}, NOW)
        self.assertEqual(st["Member baru hari ini (UTC)"], 1)
        self.assertEqual(st["Member baru 7 hari terakhir"], 3)

    def test_ends_soon_explicit_7_day_label_and_boundary(self):
        ids = [self.mem() for _ in range(4)]
        for mid, s, e in zip(ids, ("active", "active", "active", "expired"),
                             (NOW + timedelta(days=7), NOW + timedelta(days=7, seconds=1), NOW - timedelta(seconds=1), NOW + timedelta(days=1))):
            self.w.execute("INSERT INTO subscriptions(member_id,program_id,status,created_at,starts_at,ends_at) VALUES(?,2,?,'t','t',?)", (mid, s, iso(e)))
        st = pipeline.stats(self.w, {"owner"}, NOW)
        self.assertEqual(st["Program berakhir dalam 7 hari"], 1)
        self.assertIn("Program berakhir dalam 7 hari", self.own.get("/dashboard").text)

    def test_intro_and_followup_and_pending(self):
        a, b = self.mem(), self.mem()
        self.intro(a, b, ra="tertarik", rb="tidak")
        self.w.execute("UPDATE members SET next_followup_at='2026-11-07' WHERE id=?", (a,))
        self.w.execute("UPDATE members SET next_followup_at='2026-11-08' WHERE id=?", (b,))
        st = pipeline.stats(self.w, {"owner"}, NOW)
        self.assertEqual((st["Respons perkenalan: tertarik"], st["Respons perkenalan: tidak"], st["Follow-up jatuh tempo"]), (1, 1, 1))
        self.assertEqual(st["Verifikasi identitas menunggu"], 2)

    def test_role_safe(self):
        self.mem()
        self.assertEqual(pipeline.stats(self.w, {"event"}), {})
        f = pipeline.stats(self.w, {"finance"})
        self.assertEqual(list(f), ["Pembayaran menunggu verifikasi"])
        self.assertEqual(list(pipeline.stats(self.w, {"verifier"})), ["Verifikasi identitas menunggu"])
        self.assertNotIn("Member baru", self.fin.get("/dashboard").text)
        self.assertIn("Member baru hari ini", self.mm.get("/dashboard").text)
        self.assertNotIn("Pembayaran menunggu", self.mm.get("/dashboard").text)

    def test_pending_payment_counts_only_with_proof(self):
        self.paying(proof=False); self.paying()
        self.assertEqual(pipeline.stats(self.w, {"finance"})["Pembayaran menunggu verifikasi"], 1)


class TestList(PL):
    def test_roles(self):
        for cl, code in ((self.mm, 200), (self.own, 200), (self.fin, 403), (self.staff("v", "verifier"), 403), (self.staff("e", "event"), 403)):
            self.assertEqual(cl.get("/anggota").status_code, code)
        self.assertEqual(self.c.get("/anggota").headers["location"], "/login")
        self.assertEqual(self.member()[0].get("/anggota").status_code, 303)   # member session is not staff

    def test_filters_and_pagination(self):
        for i in range(23):
            self.mem(full_name=f"Nama{i:02d} Dummy", **({"gender": "Perempuan", "religion": "Kristen", "domicile": "Medan", "ethnicity": "WNA Asian"} if i < 3 else {}))
        q = lambda **a: pipeline.query(self.w, a, date(2026, 11, 7))
        rows, total, pg, pages, _ = q()
        self.assertEqual((len(rows), total, pages), (20, 23, 2))
        self.assertEqual(len(q(page="2")[0]), 3)
        self.assertEqual(q(page="99")[2], 2)       # clamped
        self.assertEqual(q(page="abc")[2], 1)
        for k, v in (("gender", "Perempuan"), ("religion", "Kristen"), ("ethnicity", "WNA Asian"), ("domicile", "medan"), ("q", "Nama01")):
            self.assertEqual(q(**{k: v})[1], 3 if k != "q" else 1, k)
        self.assertEqual(q(q="%")[1], 0)           # wildcard escaped
        self.assertEqual(q(q="x' OR 1=1 --")[1], 0)
        self.assertEqual(q(pipeline="Baru")[1], 23)   # NULL pipeline = Baru
        self.assertEqual(q(program="1")[1], 23)
        self.assertEqual(q(program="2")[1], 0)
        self.assertEqual(q(verified="1")[1], 0)
        self.assertIn("Nama00", self.mm.get("/anggota?gender=Perempuan&page=1").text)

    def test_age_and_verified_filters(self):
        a, b = self.mem(birth_date="1995-05-17"), self.mem(birth_date="2000-11-08")   # 31 and 26 on 2026-11-07
        self.w.execute("UPDATE members SET status='verified' WHERE id=?", (b,))
        ids = lambda **k: {r["id"] for r in pipeline.query(self.w, k, date(2026, 11, 7))[0]}
        self.assertEqual(ids(age_min="27"), {a})
        self.assertEqual(ids(age_max="26"), {b})
        self.assertEqual(ids(age_min="31", age_max="31"), {a})
        self.assertEqual(ids(age_min="25", age_max="25"), {b})
        self.assertEqual(ids(verified="1"), {b})
        self.assertEqual(ids(age_min="junk"), {a, b})
        self.assertEqual(db.age_on(date(2000, 11, 8), date(2026, 11, 7)), 25)
        self.assertEqual(ids(age_max="25"), {b})      # actual age 25: birthday tomorrow

    def test_feb29_cut(self):
        self.assertEqual(pipeline._cut(date(2028, 2, 29), 1), date(2027, 2, 28))


class TestPipeline(PL):
    def test_set_preserves_status_and_audit_no_pii(self):
        mid = self.mem()
        before = self.w.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()[0]
        r = self.pp(self.mm, mid, pipeline="Dalam Perkenalan", next_followup_at="2026-11-10", internal_notes="rahasia-catatan")
        self.assertEqual(r.status_code, 303)
        row = self.w.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        self.assertEqual((row["pipeline"], row["next_followup_at"], row["internal_notes"], row["status"]), ("Dalam Perkenalan", "2026-11-10", "rahasia-catatan", before))
        d = self.w.execute("SELECT staff_id,detail FROM audit_log WHERE action='pipeline_set'").fetchone()
        self.assertEqual(d["detail"], f"member={mid} to=Dalam Perkenalan")
        self.assertNotIn("rahasia", d["detail"]); self.assertNotIn("Budi", d["detail"])
        self.assertIn("Dalam Perkenalan", self.own.get("/anggota?pipeline=Dalam+Perkenalan").text)
        self.pp(self.own, mid, pipeline="Nonaktif")   # cleared follow-up/notes
        self.assertIsNone(self.w.execute("SELECT next_followup_at FROM members WHERE id=?", (mid,)).fetchone()[0])

    def test_validation_csrf_roles(self):
        mid = self.mem()
        for bad in ({"pipeline": "Hacker"}, {"pipeline": "Baru", "next_followup_at": "2026-02-30"}, {"pipeline": "Baru", "next_followup_at": "10/11/2026"},
                    {"pipeline": "Baru", "internal_notes": "x" * 1001}):
            self.assertEqual(self.pp(self.mm, mid, **bad).status_code, 400, bad)
        self.assertEqual(self.pp(self.mm, 99999, pipeline="Baru").status_code, 404)
        self.assertEqual(self.spost(self.mm, f"/anggota/{mid}/pipeline", {"pipeline": "Baru"}, csrf=False).status_code, 403)
        for cl in (self.fin, self.staff("v", "verifier")):
            self.assertEqual(self.pp(cl, mid, pipeline="Matched").status_code, 403)
        self.assertEqual(self.c.post(f"/anggota/{mid}/pipeline", data={"pipeline": "Matched"}).status_code, 303)
        self.assertIsNone(self.w.execute("SELECT pipeline FROM members WHERE id=?", (mid,)).fetchone()[0])
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='pipeline_set'").fetchone()[0], 0)

    def test_xss_escaped(self):
        mid = self.mem()
        self.pp(self.mm, mid, pipeline="Baru", internal_notes="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", self.mm.get("/anggota").text)


if __name__ == "__main__":
    unittest.main()
