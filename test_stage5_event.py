"""ACK Stage 5 chunk 1: schema v7 + event management + member registration. Synthetic data only."""
import json
import sqlite3
import threading
import unittest

import db
import events
import test_stage4_staff as t4

SCHEMA = json.dumps([{"key": "diet", "label": "Diet <b>", "type": "choice", "options": ["Halal", "Vegan"], "required": True},
                     {"key": "note", "label": "Catatan", "type": "text"}, {"key": "ok", "label": "Setuju", "type": "bool", "required": True}])


def ev(**o):
    d = dict(title="Dinner Demo", slug="dinner-demo", starts_at="2026-11-14T12:00", ends_at="2026-11-14T17:00", location="El Willy",
             dresscode="Smart casual", price_idr="290000", capacity="2", quota_m="1", quota_f="1", extra_fields="[]", active="1")
    d.update(o)
    return {k: v for k, v in d.items() if v is not None}


class Mig(unittest.TestCase):
    def test_v7_preserves_legacy_and_idempotent(self):
        c = db.connect(":memory:")
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO events(title,starts_at,capacity,price_idr,active,created_at) VALUES('a','x',5,100,1,'t'),('b',NULL,NULL,NULL,1,'t')")
        c.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES('ACK-0001','n','1990-01-01','t')")
        c.execute("INSERT INTO event_regs(event_id,member_id,status,created_at) VALUES(1,1,'weird_legacy','t')")
        c.execute("PRAGMA user_version=6")
        db.migrate(c)
        db.migrate(c)
        db.init_db(c)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual([r[0] for r in c.execute("SELECT slug FROM events ORDER BY id")], ["event-1", "event-2"])
        self.assertEqual(c.execute("SELECT price_idr,capacity,quota_m FROM events WHERE id=2").fetchone()[:], (None, None, None))
        self.assertEqual(c.execute("SELECT status,answers FROM event_regs").fetchone()[:], ("weird_legacy", "{}"))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO events(title,slug,created_at) VALUES('c','event-1','t')")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("INSERT INTO event_regs(event_id,member_id,status,created_at) VALUES(2,1,'bogus','t')")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE event_regs SET status='bogus'")
        c.execute("UPDATE event_regs SET status='paid'")
        c.close()

    def test_schema_parser(self):
        self.assertEqual(events.parse_schema("")[0], [])
        self.assertIsNone(events.parse_schema(json.dumps([{"key": "a", "label": "x", "type": "html"}]))[0])
        self.assertIsNone(events.parse_schema(json.dumps([{"key": "a", "label": "x", "type": "choice", "options": ["1"]}]))[0])
        self.assertIsNone(events.parse_schema(json.dumps([{"key": "a", "label": "x", "type": "text", "onclick": "x"}]))[0])
        self.assertIsNone(events.parse_schema(json.dumps([{"key": "a", "label": "x", "type": "text"}] * 2))[0])
        self.assertIsNone(events.parse_schema("[" * 10)[0])


class E(t4.S):
    def setUp(self):
        super().setUp()
        self.ev = self.staff("evt", "event")
        self.fin = self.staff("fin", "finance")
        self.own = self.staff("own", "owner")

    def mk(self, cl=None, **o):
        r = self.spost(cl or self.ev, "/staff/event/baru", ev(**o))
        return r

    def eid(self, slug="dinner-demo"):
        return self.w.execute("SELECT id FROM events WHERE slug=?", (slug,)).fetchone()[0]

    def reg(self, gender, slug="dinner-demo", **form):
        cl, mid = self.member(gender=gender)
        r = cl.post(f"/event/{slug}/daftar", data={"csrf_token": self.tok(cl, f"/event/{slug}"), **form})
        return cl, mid, r

    def rid(self, mid):
        return self.w.execute("SELECT id FROM event_regs WHERE member_id=?", (mid,)).fetchone()[0]

    def pick(self, cl, eid, rid):
        return self.spost(cl, f"/staff/event/{eid}/pendaftar/{rid}/pilih")

    def test_rbac_csrf(self):
        for p in ("/staff/event", "/staff/event/baru"):
            self.assertEqual(self.c.get(p).status_code, 303)
            self.assertEqual(self.fin.get(p).status_code, 403)
            self.assertEqual(self.ev.get(p).status_code, 200)
            self.assertEqual(self.own.get(p).status_code, 200)
        self.assertEqual(self.fin.post("/staff/event/baru", data=ev() | {"csrf_token": self.stok(self.fin)}).status_code, 403)
        self.assertEqual(self.ev.post("/staff/event/baru", data=ev()).status_code, 403)
        self.assertEqual(self.ev.post("/staff/event/baru", data=ev() | {"csrf_token": "x"}).status_code, 403)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)   # nothing seeded or created
        self.assertEqual(self.mk(self.own).status_code, 303)
        eid = self.eid()
        for p in (f"/staff/event/{eid}/ubah",):
            self.assertEqual(self.ev.post(p, data=ev(title="x")).status_code, 403)
            self.assertEqual(self.fin.post(p, data=ev(title="x") | {"csrf_token": self.stok(self.fin)}).status_code, 403)

    def test_validation_and_edit(self):
        for bad in (dict(slug="Bad Slug"), dict(slug="a"), dict(title=""), dict(starts_at="14-11-2026"), dict(ends_at="2026-11-14T11:00"),
                    dict(price_idr="-5"), dict(price_idr="1e3"), dict(capacity="abc"), dict(quota_m="3"), dict(extra_fields="{bad")):
            self.assertEqual(self.mk(**bad).status_code, 400, bad)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.mk(price_idr="").status_code, 303)
        self.assertIsNone(self.w.execute("SELECT price_idr FROM events").fetchone()[0])   # unconfigured, no invented price
        self.assertEqual(self.mk().status_code, 400)   # duplicate slug
        eid = self.eid()
        self.assertEqual(self.spost(self.ev, f"/staff/event/{eid}/ubah", ev(location="Baru")).status_code, 303)
        self.assertEqual(self.w.execute("SELECT location FROM events").fetchone()[0], "Baru")
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('event_create','event_update')").fetchone()[0], 2)

    def test_public_escaped_and_register(self):
        self.mk(title="<script>x</script>", extra_fields=SCHEMA)
        r = self.c.get("/event/dinner-demo")
        self.assertIn("&lt;script&gt;", r.text)
        self.assertNotIn("<script>x", r.text)
        self.assertIn("DATA DUMMY", r.text)
        self.assertEqual(self.c.get("/event/none").status_code, 404)
        cl, mid = self.member()
        page = cl.get("/event/dinner-demo").text
        self.assertIn("Diet &lt;b&gt;", page)
        tok = self.tok(cl, "/event/dinner-demo")
        self.assertEqual(cl.post("/event/dinner-demo/daftar", data={"q_diet": "Halal", "q_ok": "ya"}).status_code, 403)   # CSRF
        for form in ({}, {"q_diet": "Pizza", "q_ok": "ya"}, {"q_diet": "Halal"}):
            self.assertEqual(cl.post("/event/dinner-demo/daftar", data={"csrf_token": tok} | form).status_code, 400, form)
        ok = {"csrf_token": tok, "q_diet": "Halal", "q_ok": "ya", "q_note": "hai"}
        self.assertEqual(cl.post("/event/dinner-demo/daftar", data=ok).status_code, 200)
        self.assertEqual(cl.post("/event/dinner-demo/daftar", data=ok | {"q_diet": "Vegan"}).status_code, 200)   # idempotent: no overwrite
        row = self.w.execute("SELECT status,answers FROM event_regs").fetchall()
        self.assertEqual(len(row), 1)
        self.assertEqual(json.loads(row[0][1])["diet"], "Halal")
        # anonymous cannot register
        self.assertEqual(self.c.post("/event/dinner-demo/daftar", data={}).status_code, 303)

    def test_age_gate(self):
        self.mk()
        cl, mid = self.member()
        self.w.execute("UPDATE members SET birth_date=? WHERE id=?", (f"{db.date.today().year - 17}-01-01", mid))
        tok = self.tok(cl, "/event/dinner-demo")
        self.assertEqual(cl.post("/event/dinner-demo/daftar", data={"csrf_token": tok}).status_code, 403)
        self.w.execute("UPDATE members SET birth_date='1990' WHERE id=?", (mid,)) if False else None
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM event_regs").fetchone()[0], 0)

    def test_selection_missing_config_blocks(self):
        for i, o in enumerate((dict(price_idr=""), dict(capacity=""), dict(quota_m=""))):
            slug = f"cfg-{i}"
            self.assertEqual(self.mk(slug=slug, **o).status_code, 303)
            _, mid, _r = self.reg("Laki-laki", slug)
            r = self.pick(self.ev, self.eid(slug), self.rid(mid))
            self.assertEqual(r.status_code, 409)
            self.assertEqual(self.w.execute("SELECT status FROM event_regs WHERE member_id=?", (mid,)).fetchone()[0], "registered")

    def test_overbook_quota_cancel_release(self):
        self.mk(capacity="2", quota_m="1", quota_f="2")
        eid = self.eid()
        _, m1, _r = self.reg("Laki-laki")
        _, m2, _r = self.reg("Laki-laki")
        _, f1, _r = self.reg("Perempuan")
        _, f2, _r = self.reg("Perempuan")
        self.assertEqual(self.pick(self.ev, eid, self.rid(m1)).status_code, 200)
        self.assertEqual(self.pick(self.ev, eid, self.rid(m1)).status_code, 409)   # re-select no-op
        self.assertEqual(self.pick(self.ev, eid, self.rid(m2)).status_code, 409)   # male quota 1
        self.assertEqual(self.pick(self.ev, eid, self.rid(f1)).status_code, 200)
        self.assertEqual(self.pick(self.ev, eid, self.rid(f2)).status_code, 409)   # capacity 2
        sel = self.w.execute("SELECT selected_at,locked_price_idr FROM event_regs WHERE member_id=?", (m1,)).fetchone()
        self.assertEqual(sel[1], 290000)
        self.assertTrue(sel[0])
        self.spost(self.ev, f"/staff/event/{eid}/ubah", ev(price_idr="999", capacity="2", quota_m="1", quota_f="2"))   # price change keeps lock
        self.assertEqual(self.w.execute("SELECT locked_price_idr FROM event_regs WHERE member_id=?", (m1,)).fetchone()[0], 290000)
        self.assertEqual(self.spost(self.ev, f"/staff/event/{eid}/ubah", ev(capacity="1", quota_m="1", quota_f="1")).status_code, 400)  # below held
        self.assertEqual(self.pick(self.fin, eid, self.rid(m2)).status_code, 403)
        self.assertEqual(self.spost(self.ev, f"/staff/event/{eid}/pendaftar/{self.rid(m1)}/batal").status_code, 200)
        self.assertEqual(self.pick(self.ev, eid, self.rid(m2)).status_code, 200)   # explicit release freed the seat

    def test_concurrent_selection_never_overbooks(self):
        self.mk(capacity="1", quota_m="1", quota_f="1")
        eid = self.eid()
        ids = [self.rid(self.reg("Laki-laki")[1]) for _ in range(4)]
        res = []
        def go(mid):
            c = sqlite3.connect(self.dbp, isolation_level=None, timeout=30)
            c.row_factory = sqlite3.Row
            try:
                res.append(events.select_seat(c, eid, mid)[0])
            finally:
                c.close()
        ts = [threading.Thread(target=go, args=(m,)) for m in ids]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(res), 1)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM event_regs WHERE status='selected'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
