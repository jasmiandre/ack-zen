"""Stage 4 ops: WA links, Silver card, CSV import/export, audit paging. DUMMY data only."""
import csv
import io
import unittest
from urllib.parse import unquote

import db
import ops
import test_stage4_staff as ts

HDR = "Nama,Lahir,HP,Surel\n"


class O(ts.S):
    def setUp(self):
        super().setUp()
        self.own, self.mm, self.fin = self.staff("ow", "owner"), self.staff("mm", "matchmaker"), self.staff("fi", "finance")

    def imp(self, body, action="preview", cl=None):
        cl = cl or self.own
        return cl.post("/owner/impor", data={"csrf_token": self.stok(cl), "action": action, "map_full_name": "Nama", "map_birth_date": "Lahir",
                                             "map_phone": "HP", "map_email": "Surel"}, files={"file": ("a.csv", body.encode(), "text/csv")})

    def n(self):
        return self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0]


class TestCsv(O):
    def test_formula_neutralised(self):
        for v in ("=1+1", "+1", "-1", "@x", " =x", "\t=x", "\n=x"):
            self.assertTrue(ops.csv_safe(v).startswith("'"), v)
        self.assertEqual(ops.csv_safe("Budi"), "Budi")

    def test_import_preview_writes_nothing_commit_dedupes_and_gates_age(self):
        body = HDR + ("Ani Dummy,1990-01-02,081200000001,ani@example.test\n"
                      "Kecil Dummy,2015-01-02,081200000002,kecil@example.test\n"
                      "Tanpa Lahir,1990,081200000003,x@example.test\n"
                      "Dobel,1990-01-02,0812-0000-0001,dobel@example.test\n")
        before = self.n()
        r = self.imp(body)
        self.assertEqual((r.status_code, self.n()), (200, before))
        self.assertIn("1 diterima, 3 ditolak", r.text)
        self.assertNotIn("ani@example.test", r.text)
        self.imp(body, "commit")
        self.assertEqual(self.n(), before + 1)
        row = self.w.execute("SELECT status,perlu_review,phone FROM members WHERE email_key='ani@example.test'").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("pending", 1, "6281200000001"))
        self.assertIn("accepted=1 rejected=3", self.w.execute("SELECT detail FROM audit_log WHERE action='member_import'").fetchone()[0])
        self.imp(body, "commit")   # re-run: existing constraint dedupe
        self.assertEqual(self.n(), before + 1)

    def test_import_owner_only_and_csrf(self):
        self.assertEqual(self.imp(HDR, cl=self.mm).status_code, 403)
        self.assertEqual(self.own.post("/owner/impor", data={"action": "commit"}).status_code, 403)
        self.assertEqual(self.imp("Bad,Header\n").status_code, 400)

    def test_export_owner_only_neutralised_no_secrets(self):
        self.imp(HDR + "=cmd Dummy,1990-01-02,081200000009,z@example.test\n", "commit")
        self.assertEqual(self.mm.post("/owner/ekspor", data={"csrf_token": self.stok(self.mm)}).status_code, 403)
        self.assertEqual(self.own.get("/owner/ekspor").status_code, 405)
        r = self.own.post("/owner/ekspor", data={"csrf_token": self.stok(self.own)})
        rows = list(csv.reader(io.StringIO(r.text)))
        self.assertNotIn("password_hash", rows[0])
        self.assertIn("'=cmd Dummy", [c for row in rows for c in row])
        self.assertNotIn("argon2", r.text)
        self.assertEqual(self.w.execute("SELECT detail FROM audit_log WHERE action='member_export'").fetchone()[0], f"rows={len(rows) - 1}")


class TestWa(O):
    def test_role_templates_and_link(self):
        _, mid = self.member()
        code = self.w.execute("SELECT code FROM members WHERE id=?", (mid,)).fetchone()[0]
        self.assertEqual(len(self.w.execute("SELECT * FROM wa_templates").fetchall()), len(db.WA_TEMPLATES))
        t = self.mm.get(f"/wa/{mid}").text
        self.assertIn("welcome", t); self.assertNotIn("bayar_pengingat", t)
        self.assertIn("bayar_pengingat", self.fin.get(f"/wa/{mid}").text)
        url = __import__("re").search(r'href="(https://wa\.me/[^"]+)"', t).group(1).replace("&amp;", "&")
        self.assertTrue(url.startswith("https://wa.me/62812300"))
        self.assertIn(code, unquote(url))
        self.assertEqual(self.c.get(f"/wa/{mid}").headers["location"], "/login")
        self.assertEqual(self.staff("ev", "event").get(f"/wa/{mid}").status_code, 200)


class TestCard(O):
    def test_gate_and_png(self):
        cl, mid = self.member(program="3")
        self.assertEqual(self.mm.get(f"/kartu/{mid}").status_code, 409)   # not verified
        self.w.execute("UPDATE members SET status='verified' WHERE id=?", (mid,))
        r = self.mm.get(f"/kartu/{mid}")
        self.assertEqual((r.status_code, r.headers["content-type"], r.content[:4]), (200, "image/png", b"\x89PNG"))
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (1080, 1350))
        self.assertEqual(self.fin.get(f"/kartu/{mid}").status_code, 403)
        self.w.execute("UPDATE members SET publish_consent='tidak' WHERE id=?", (mid,))
        self.assertEqual(self.mm.get(f"/kartu/{mid}").status_code, 409)
        self.w.execute("UPDATE members SET publish_consent='ya' WHERE id=?", (mid,))
        self.w.execute("UPDATE subscriptions SET program_id=6 WHERE member_id=?", (mid,))   # Private Concierge never
        self.assertEqual(self.mm.get(f"/kartu/{mid}").status_code, 409)
        self.w.execute("UPDATE subscriptions SET program_id=1 WHERE member_id=?", (mid,))   # other programs not assumed
        self.assertEqual(self.mm.get(f"/kartu/{mid}").status_code, 409)


class TestAudit(O):
    def test_paging_filter_bounded(self):
        for i in range(120):
            db.audit(self.w, None, "x_demo" if i % 2 else "y_demo", f"i={i}")
        t = self.own.get("/owner/audit?page=2&action=x_demo").text
        self.assertEqual(t.count("<td>x_demo</td>"), 10)
        self.assertNotIn("y_demo</td>", t)
        self.assertLessEqual(self.own.get("/owner/audit").text.count("<tr>"), 51 + 1)
        self.assertEqual(self.own.get("/owner/audit?page=abc&action=%27%22").status_code, 200)


if __name__ == "__main__":
    unittest.main()
