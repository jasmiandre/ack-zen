"""ACK quote protection tests: subscription quote immutability. Synthetic data only."""
import re
import sqlite3
import unittest

import db
import test_stage3 as t3


class QuoteProtection(t3.P):
    def test_null_quote_stays_unconfigured_after_program_price_set(self):
        """Old pending subscription created while price NULL must not get repriced when Owner sets a price later."""
        cl, mid = self.member(program="2")          # program 2 price NULL at registration: no invoice
        self.assertIsNone(self.pay(mid))
        self.w.execute("UPDATE programs SET price_idr=349000 WHERE id=2")   # Owner configures price afterwards
        r = cl.get("/akun")
        self.assertIsNone(self.pay(mid))            # no invoice appeared out of thin air
        self.assertNotIn("Rp 349.000", r.text)      # never show new program price as this member\'s quote
        self.assertIn("info harga via admin", r.text.lower())
        sub = self.w.execute("SELECT quote_price_idr FROM subscriptions WHERE member_id=?", (mid,)).fetchone()
        self.assertIsNone(sub["quote_price_idr"])   # legacy row keeps NULL quote (distinct from new quote 0)

    def test_new_registration_snapshots_quote(self):
        """New registration on a priced program records the quote at signup; later program change cannot reprice it."""
        self.w.execute("UPDATE programs SET price_idr=349000 WHERE id=2")
        cl, mid = self.member(program="2")
        pay = self.pay(mid)
        self.assertEqual(pay["amount_idr"], 349000)
        sub = self.w.execute("SELECT quote_price_idr FROM subscriptions WHERE member_id=?", (mid,)).fetchone()
        self.assertEqual(sub["quote_price_idr"], 349000)
        self.w.execute("UPDATE programs SET price_idr=500000 WHERE id=2")   # Owner reprices program
        r = cl.get("/akun")
        pay2 = self.pay(mid)
        self.assertEqual(pay2["amount_idr"], 349000)
        self.assertIn("Rp 349.", r.text)            # total still amount+unique from the signup invoice, not repriced

    def test_priced_invoice_retains_price_across_program_config_change(self):
        """Existing paid/pending invoice keeps its amount; program_config change never touches payments."""
        self.w.execute("UPDATE programs SET price_idr=349000 WHERE id=2")
        cl, mid = self.member(program="2")
        pid = self.pay(mid)["id"]
        self.w.execute("UPDATE programs SET price_idr=900000, duration_days=30 WHERE id=2")
        self.assertEqual(self.pay(mid)["amount_idr"], 349000)
        # finance queue still shows the original amount
        s = self.staff_finance()
        row = self.w.execute("SELECT amount_idr FROM payments WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["amount_idr"], 349000)

    def staff_finance(self):
        import auth, test_stage2 as t2
        from fastapi.testclient import TestClient
        h = auth.hash_password("dummy-staff-Passw0rd")
        sid = self.w.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,'t')",
                             ("fin", "fin", h)).lastrowid
        self.w.execute("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", (sid, "finance"))
        cl = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        cl.__enter__()
        self.addCleanup(cl.__exit__)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', cl.get("/login").text).group(1)
        self.assertEqual(cl.post("/login", data={"username": "fin", "password": "dummy-staff-Passw0rd", "csrf_token": tok}).status_code, 303)
        r = cl.get("/keuangan")
        self.assertIn("Rp 349.000", r.text)
        return cl

    def test_unconfigured_quote_never_activates_via_finance_approve(self):
        """Payment with NULL amount (legacy row) must never activate a subscription via approve."""
        self.w.execute("UPDATE programs SET price_idr=349000, duration_days=30 WHERE id=2")
        cl, mid = self.member(program="2")
        pid = self.pay(mid)["id"]
        self.w.execute("UPDATE payments SET amount_idr=NULL WHERE id=?", (pid,))   # simulate legacy NULL-amount row
        self.w.execute("UPDATE payments SET proof_file_id=1 WHERE id=?", (pid,))
        self.w.execute("UPDATE users_staff SET username='fin2' WHERE username='fin'")
        import auth
        h = auth.hash_password("dummy-staff-Passw0rd")
        sid = self.w.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,'t')",
                             ("own", "own", h)).lastrowid
        self.w.execute("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", (sid, "owner"))
        from fastapi.testclient import TestClient
        cl2 = TestClient(self.app, follow_redirects=False, raise_server_exceptions=False)
        cl2.__enter__()
        self.addCleanup(cl2.__exit__)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', cl2.get("/login").text).group(1)
        self.assertEqual(cl2.post("/login", data={"username": "own", "password": "dummy-staff-Passw0rd", "csrf_token": tok}).status_code, 303)
        tok2 = re.search(r'name="csrf_token" value="([^"]+)"', cl2.get(f"/keuangan").text).group(1)
        r = cl2.post(f"/keuangan/{pid}/setuju", data={"csrf_token": tok2}, follow_redirects=False)
        self.assertIn(r.status_code, (303, 409))
        st = self.w.execute("SELECT p.status pstat, s.status sstat FROM payments p JOIN subscriptions s ON s.id=p.subscription_id WHERE p.id=?", (pid,)).fetchone()
        self.assertEqual(st["sstat"], "pending")    # subscription NOT activated


if __name__ == "__main__":
    unittest.main()
