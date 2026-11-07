"""ACK Stage 5 chunk 3: QR ticket + staff check-in. Synthetic data only. No QR decoder (zbar/cv2) on the box: SVG is checked structurally."""
import threading

from fastapi.testclient import TestClient
import unittest

import test_stage5_event as t5


class QR(t5.E):
    locals().update({n: None for n in dir(t5.E) if n.startswith("test_")})   # run only this chunk's tests, not inherited ones

    def setUp(self):
        super().setUp()
        self.mk()
        self.e = self.eid()

    def paid(self, gender="Laki-laki"):
        cl, mid, _ = self.reg(gender)
        rid = self.rid(mid)
        self.w.execute("UPDATE event_regs SET status='paid' WHERE id=?", (rid,))
        return cl, mid, rid

    def qr(self, rid):
        return self.w.execute("SELECT qr_token FROM event_regs WHERE id=?", (rid,)).fetchone()[0]

    def ticket(self, cl, rid):
        return cl.get(f"/akun/event/{rid}/tiket.svg")

    def scan(self, cl, token, eid=None, js=True):
        return self.spost(cl, f"/staff/event/{eid or self.e}/checkin", {"token": token})

    def test_ticket_only_paid_owner_nostore(self):
        cl, mid, rid = self.reg("Laki-laki")[0], None, None
        rid = self.rid(self.w.execute("SELECT member_id FROM event_regs").fetchone()[0])
        self.assertEqual(self.ticket(cl, rid).status_code, 404)   # registered
        self.w.execute("UPDATE event_regs SET status='paid' WHERE id=?", (rid,))
        r = self.ticket(cl, rid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("<svg", r.text)
        tok = self.qr(rid)
        self.assertGreaterEqual(len(tok), 32)   # token_urlsafe(24) = 192 bit
        self.assertNotIn(tok, r.text)           # token is encoded in modules, not echoed as text
        self.ticket(cl, rid)
        self.assertEqual(self.qr(rid), tok)     # persistent, not regenerated
        other, _, _ = self.reg("Perempuan")
        self.assertEqual(self.ticket(other, rid).status_code, 404)   # IDOR
        self.assertEqual(self.c.get(f"/akun/event/{rid}/tiket.svg").status_code, 404)   # anonymous
        self.assertEqual(self.ticket(cl, "abc").status_code, 404)
        self.assertIn("tiket.svg", cl.get(f"/akun/event/{rid}").text)

    def test_manual_code_reveal_fallback(self):
        cl, mid, rid = self.paid()
        page = cl.get(f"/akun/event/{rid}").text
        self.assertIn('id="showcode"', page)
        self.assertIn(f'/akun/event/{rid}/kode', page)      # URL has no token
        r = cl.get(f"/akun/event/{rid}/kode")
        self.assertEqual((r.status_code, r.headers["cache-control"]), (200, "no-store"))
        tok = self.qr(rid)
        self.assertEqual(r.text, tok)
        self.assertNotIn(tok, page)                          # never rendered until explicit click
        self.assertEqual(self.scan(self.ev, r.text).status_code, 200)   # the revealed code actually checks in
        other, _, _ = self.paid("Perempuan")
        self.assertEqual(other.get(f"/akun/event/{rid}/kode").status_code, 404)    # owner only
        self.assertEqual(TestClient(self.app).get(f"/akun/event/{rid}/kode").status_code, 404)
        self.w.execute("UPDATE event_regs SET status='selected' WHERE id=?", (rid,))
        self.assertEqual(cl.get(f"/akun/event/{rid}/kode").status_code, 404)    # not paid

    def test_checkin_manual_form_and_scripts_served(self):
        cl, mid, rid = self.paid()
        page = self.ev.get(f"/staff/event/{self.e}/checkin").text
        self.assertIn('id="token"', page)
        self.assertIn('id="manual"', page)
        self.assertEqual(TestClient(self.app).get("/static/js/ticketcode.js").status_code, 200)

    def test_checkin_once_idempotent_and_audit_clean(self):
        cl, mid, rid = self.paid()
        self.ticket(cl, rid)
        tok = self.qr(rid)
        self.assertEqual(self.scan(self.ev, tok).status_code, 200)
        self.assertEqual(self.w.execute("SELECT status FROM event_regs WHERE id=?", (rid,)).fetchone()[0], "checked_in")
        r = self.scan(self.ev, tok)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sudah check-in", r.text)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='event_checkin'").fetchone()[0], 1)
        self.assertNotIn(tok, " ".join(x[0] for x in self.w.execute("SELECT detail FROM audit_log")))
        self.assertEqual(self.ticket(cl, rid).status_code, 200)   # still viewable when checked_in
        self.assertEqual(self.scan(self.own, tok).status_code, 200)

    def test_blocked_cases(self):
        cl, mid, rid = self.paid()
        self.ticket(cl, rid)
        tok = self.qr(rid)
        for bad in ("", "x", "a" * 40, tok[:-1] + ("A" if tok[-1] != "A" else "B"), "../etc", tok + "!"):
            self.assertEqual(self.scan(self.ev, bad).status_code, 400, bad)
        self.mk(slug="other-ev")
        self.assertEqual(self.scan(self.ev, tok, self.eid("other-ev")).status_code, 409)   # event mismatch
        self.w.execute("UPDATE events SET active=0 WHERE id=?", (self.e,))
        self.assertEqual(self.scan(self.ev, tok).status_code, 409)
        self.w.execute("UPDATE events SET active=1 WHERE id=?", (self.e,))
        for st in ("selected", "proof_uploaded", "cancelled"):
            self.w.execute("UPDATE event_regs SET status=? WHERE id=?", (st, rid))
            self.assertEqual(self.scan(self.ev, tok).status_code, 409, st)
        self.assertEqual(self.w.execute("SELECT status FROM event_regs WHERE id=?", (rid,)).fetchone()[0], "cancelled")

    def test_rbac_csrf_and_get_no_mutation(self):
        cl, mid, rid = self.paid()
        self.ticket(cl, rid)
        tok = self.qr(rid)
        p = f"/staff/event/{self.e}/checkin"
        self.assertEqual(self.c.get(p).status_code, 303)
        self.assertEqual(self.fin.get(p).status_code, 403)
        self.assertEqual(self.ev.get(p).status_code, 200)
        self.assertEqual(self.fin.post(p, data={"token": tok, "csrf_token": self.stok(self.fin)}).status_code, 403)   # finance cannot
        self.assertEqual(self.ev.post(p, data={"token": tok}).status_code, 403)                                          # no CSRF
        self.assertEqual(cl.post(p, data={"token": tok}).status_code in (303, 403), True)                                # member cookie is not staff
        self.ev.get(p + f"?token={tok}")
        self.assertEqual(self.w.execute("SELECT status FROM event_regs WHERE id=?", (rid,)).fetchone()[0], "paid")
        self.assertNotIn(tok, self.ev.get(p).text)

    def test_race_double_checkin_once(self):
        cl, mid, rid = self.paid()
        self.ticket(cl, rid)
        tok = self.qr(rid)
        outs = []
        ths = [threading.Thread(target=lambda: outs.append(self.scan(self.ev, tok).text)) for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM audit_log WHERE action='event_checkin'").fetchone()[0], 1)
        self.assertEqual(sum("Check-in berhasil" in o for o in outs), 1)

    def test_tokens_unique_and_page_self_hosted(self):
        toks = set()
        for g in ("Laki-laki", "Perempuan"):
            cl, _, rid = self.paid(g)
            self.ticket(cl, rid)
            toks.add(self.qr(rid))
        self.assertEqual(len(toks), 2)
        page = self.ev.get(f"/staff/event/{self.e}/checkin").text
        self.assertIn("/static/js/checkin.js", page)
        self.assertNotRegex(page, r"https?://")
        self.assertIn('type="password"', page)


if __name__ == "__main__":
    unittest.main()
