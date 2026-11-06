"""ACK Stage 1 tests (stdlib unittest + TestClient). Public landing, content.json, CSP, static safety, Stage 0 preserved."""
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import landing
from app import create_app

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "content.json"), encoding="utf-8") as _f:
    CONTENT = json.load(_f)


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(db_path=os.path.join(self.tmp.name, "t.db"), secret_key="test-secret-key-0123456789abcdef")
        self.c = TestClient(self.app, follow_redirects=False)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__)
        self.addCleanup(self.tmp.cleanup)
        self.html = self.c.get("/").text


class TestLanding(Base):
    def test_root_is_public_landing_not_redirect(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<h1", r.text)

    def test_stage0_routes_preserved(self):
        for p in ("/dashboard", "/owner/staff", "/owner/audit", "/access/finance"):
            self.assertEqual((self.c.get(p).status_code, self.c.get(p).headers["location"]), (303, "/login"), p)
        self.assertEqual(self.c.get("/login").status_code, 200)
        self.assertEqual(self.c.get("/health").json(), {"status": "ok", "stage": 0, "db": True})

    def test_all_sections_present(self):
        for sid in ("atas", "kenapa", "cara-kerja", "program", "event", "privasi", "faq", "h-stats", "h-story", "h-founder", "h-close"):
            self.assertIn(f'id="{sid}"', self.html, sid)
        self.assertEqual(len(re.findall(r'<li class="prog[ "]', self.html)), 6)
        self.assertEqual(len(re.findall(r"<details>", self.html)), len(CONTENT["faq"]["items"]))

    def test_every_content_string_is_rendered_escaped(self):
        import html as h
        skip = ("meta", "reference_only_inactive", "date_iso", "countdown_expired", "countdown_units", "legal", "cta_demo", "cta_primary_demo", "whatsapp")
        miss = []
        for k, v in CONTENT.items():
            if k in ("meta", "legal", "whatsapp"):
                continue
            for s in strings(v):
                if "http" in s or s.startswith("2026-"):
                    continue
                if h.escape(s).replace("&#x27;", "&#39;") not in self.html and h.escape(s) not in self.html and h.escape(s, quote=False) not in self.html and s not in self.html:
                    miss.append(s)
        miss = [m for m in miss if not any(m == d for d in strings(CONTENT["event"]["cta_demo"]))]
        self.assertEqual([m for m in miss if m not in strings(CONTENT["ui"])], [], "content strings not rendered")

    def test_xss_in_content_escaped(self):
        evil = json.loads(json.dumps(CONTENT))
        evil["hero"]["title"] = '<script>alert(1)</script>"><img src=x onerror=1>'
        evil["faq"]["items"][0]["q"] = "<b onmouseover=1>x</b>"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as f:
                json.dump(evil, f)
            c = landing.load_content(p)
        env = self.app.router  # render via the app's own jinja env through a temp swap of content
        orig = landing.load_content
        landing.load_content = lambda path=None: c
        try:
            t = self.c.get("/").text
        finally:
            landing.load_content = orig
        self.assertNotIn("<script>alert(1)", t)
        self.assertNotIn("<img src=x", t)
        self.assertIn("&lt;script&gt;", t)
        self.assertNotIn("<b onmouseover", t)

    def test_no_fabricated_proof(self):
        for item in CONTENT["stats"]["items"]:
            self.assertTrue(item["value"].startswith("[") and "konfirmasi klien" in item["value"], item)
            self.assertFalse(re.search(r"\d", item["value"]), item)
        self.assertEqual(len(CONTENT["stories"]["items"]), 1)
        self.assertTrue(CONTENT["stories"]["items"][0]["quote"].startswith("[TESTIMONI ASLI"))
        self.assertTrue(CONTENT["founder"]["name"].startswith("["))
        for p in CONTENT["programs"]["items"]:
            self.assertIsNone(p["price"]); self.assertIsNone(p["duration_days"]); self.assertIsNone(p["intro_quota"])
        self.assertEqual(sum(p["status"] == "existing" for p in CONTENT["programs"]["items"]), 4)
        self.assertEqual(sum(p["status"] == "proposed" for p in CONTENT["programs"]["items"]), 2)
        self.assertIn("Menunggu konfirmasi klien", self.html)

    def test_logo_local_not_hotlinked(self):
        self.assertIn("lgo-nav", self.html)  # inline SVG concept logo; original PNG archived, unused
        self.assertNotIn("msha.ke", self.html)
        self.assertEqual(self.c.get("/static/img/favicon.svg").status_code, 200)
        self.assertEqual(self.c.get("/static/img/favicon-32.png").status_code, 200)
        self.assertGreaterEqual(self.html.count("class=\"lgo "), 2)

    def test_external_links_allowlist_and_event_facts(self):
        allow = {"https://www.instagram.com/akucarikamu.id", "https://www.tiktok.com/@akucarikamu.id",
                 "https://whatsapp.com/channel/0029Vb98jsT5vK9ziES81z20"}
        tags = re.findall(r"<a\b[^>]*href=\"(https?://[^\"]+)\"[^>]*>", self.html)
        self.assertEqual(set(tags), allow)
        for m in re.finditer(r"<a\b[^>]*href=\"https?://[^>]*>", self.html):
            self.assertIn('target="_blank"', m.group(0))
            self.assertIn("noopener", m.group(0))
        self.assertNotIn("wa.me", self.html)
        self.assertNotIn("0878", self.html)
        self.assertNotRegex(self.html, r"(?i)<(img|script|link)[^>]+(src|href)=\"https?://(?!cdn\.jsdelivr)")
        self.assertNotIn("googleapis", self.html)
        for s in ("12.00–17.00 WIB", "El Willy Padel, Meruya, Jakarta Barat", "Rp290.000", "40 seat", "14 November 2026",
                  "sneakers", "tanpa heels", "Speed dating, padel, snack"):
            self.assertIn(s, self.html, s)
        self.assertNotRegex(self.html, r"(?i)sisa kuota|per gender")
        self.assertIn("Tempat dimana para \u201cAku\u201d menCari sang \u201cKamu\u201d.", self.html)
        self.assertNotIn("Masuk staf", self.html)
        self.assertEqual(self.c.get("/login").status_code, 200)
        self.assertEqual(self.html.count("demo-banner"), 1)
        self.assertNotRegex(self.html, r"(?i)direncanakan|belum aktif|rencana:")

    def test_demo_cta_and_legal_are_honest(self):
        self.assertIn('id="dlg-daftar"', self.html)
        self.assertIn("Tahap 2", self.html)
        self.assertIn("Tahap 5", self.html)
        self.assertIn("DRAF", self.html)
        self.assertNotRegex(self.html, r"(?i)\b\d+\s*(hari|bulan|tahun)\b.*(simpan|retensi)")

    def test_jinja_template_not_hiding_content_until_js(self):
        self.assertNotRegex(self.html, r'(?i)style="[^"]*(display:\s*none|opacity:\s*0)')
        self.assertNotIn("\nhidden", self.html)
        self.assertIn("<script src=\"/static/js/landing.js\" defer>", self.html)

    def test_a11y_basics(self):
        self.assertIn('lang="id"', self.html)
        self.assertEqual(self.html.count("<h1"), 1)
        self.assertIn('class="skip"', self.html)
        self.assertEqual(len(re.findall(r"<main", self.html)), 1)
        for m in re.finditer(r"<img\b[^>]*>", self.html):
            self.assertIn("alt=", m.group(0))
        ids = re.findall(r'\sid="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")


class TestHeadersAndStatic(Base):
    def test_public_csp_pinned(self):
        csp = self.c.get("/").headers["content-security-policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("*", csp)
        for u in ("three@0.160.0", "gsap@3.12.5/dist/gsap.min.js", "ScrollTrigger.min.js", "lenis@1.1.13"):
            self.assertIn(u, csp)
        self.assertIn("default-src 'none'", csp)

    def test_staff_csp_still_tight(self):
        csp = self.c.get("/login").headers["content-security-policy"]
        self.assertEqual(csp, "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.assertIn("no-store", self.c.get("/login").headers["cache-control"])
        self.assertNotIn("cdn.jsdelivr", self.c.get("/login").text)

    def test_static_assets_served(self):
        for p, ct in (("/static/css/landing.css", "text/css"), ("/static/js/landing.js", "javascript"),
                      ("/static/fonts/inter-latin-wght-normal.woff2", "font")):
            r = self.c.get(p)
            self.assertEqual(r.status_code, 200, p)
            self.assertIn(ct, r.headers["content-type"], p)

    def test_secrets_and_data_not_reachable(self):
        for p in ("/static/.env", "/static/css/../../.env", "/static/css/%2e%2e/%2e%2e/.env", "/static/../data/ack.db",
                  "/static/", "/static/x", "/.env", "/data/ack.db", "/content.json", "/app.py", "/static/fonts/../../seed_credentials.txt",
                  "/static/css/..%2f..%2f.env", "/static/css/%2e%2e%2f%2e%2e%2f.env"):
            r = self.c.get(p)
            self.assertIn(r.status_code, (404, 400, 307, 308), p)
            self.assertNotIn("SECRET_KEY", r.text, p)

    def test_staticdirs_contain_only_assets(self):
        for sub, exts in (("css", {".css"}), ("js", {".js"}), ("fonts", {".woff2"}), ("img", {".png", ".svg"})):
            for f in os.listdir(os.path.join(HERE, "static", sub)):
                self.assertIn(os.path.splitext(f)[1], exts, f)

    def test_js_has_sri_and_reduced_motion_guard(self):
        with open(os.path.join(HERE, "static/js/landing.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertEqual(len(re.findall(r"sha384-", js)), 3)
        self.assertIn("prefers-reduced-motion", js)
        self.assertIn("hardwareConcurrency", js)
        self.assertIn("webglcontextlost", js)
        self.assertLess(js.index("if (reduce)"), js.index("loadScript(libs.gsap)"))
        self.assertNotIn("wa.me", js)


class TestCountdown(unittest.TestCase):
    ISO = CONTENT["event"]["date_iso"]

    def test_exact(self):
        t = datetime.fromisoformat(self.ISO)
        r = landing.countdown(self.ISO, t - timedelta(days=2, hours=3, minutes=4, seconds=5))
        self.assertEqual((r["d"], r["h"], r["m"], r["s"], r["expired"]), (2, 3, 4, 5, False))

    def test_expired_and_boundary(self):
        t = datetime.fromisoformat(self.ISO)
        self.assertTrue(landing.countdown(self.ISO, t)["expired"])
        self.assertTrue(landing.countdown(self.ISO, t + timedelta(days=9))["expired"])
        self.assertFalse(landing.countdown(self.ISO, t - timedelta(seconds=1))["expired"])

    def test_timezone_is_jakarta(self):
        self.assertTrue(self.ISO.endswith("+07:00"))
        self.assertEqual(datetime.fromisoformat(self.ISO).utcoffset(), timedelta(hours=7))


class TestContentJson(unittest.TestCase):
    def test_parses_and_complete(self):
        for k in ("ui", "nav", "hero", "stats", "why", "how", "programs", "event", "stories", "privacy", "founder", "faq", "closing", "whatsapp", "footer", "legal"):
            self.assertIn(k, CONTENT)
        self.assertEqual(len(CONTENT["programs"]["items"]), 6)
        self.assertEqual(len(CONTENT["why"]["items"]), 4)
        self.assertEqual(len(CONTENT["how"]["steps"]), 4)
        self.assertEqual(len(CONTENT["faq"]["items"]), 6)
        self.assertEqual(len(CONTENT["event"]["sponsors"]), 7)

    def test_bad_json_fails_loud(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{bad")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(json.JSONDecodeError):
            landing.load_content(f.name)



class Rev2(Base):
    """Hero revision (10 points)."""
    def css(self):
        return self.c.get("/static/css/landing.css").text

    def test_r1_fonts_selfhost_swap_preload(self):
        css = self.css()
        self.assertNotIn("font-display:optional", css)
        self.assertEqual(css.count("font-display:swap"), 3)
        for f in ("playfair-display-latin-wght-normal", "playfair-display-latin-wght-italic"):
            self.assertIn("/static/fonts/%s.woff2" % f, self.html)
            r = self.c.get("/static/fonts/%s.woff2" % f)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content[:4], b"wOF2")
        self.assertNotRegex(css, r"url\(https?:")

    def test_r2_r5_heart_small_no_black_edge_soft_glow(self):
        css = self.css()
        self.assertNotIn("-edge", css + self.html)
        self.assertNotIn("#1a0610", css)
        self.assertIn("drop-shadow", css)
        self.assertRegex(css, r"max-width:899px\)\{[^@]*\.hero\{height:auto\}")

    def test_r3_no_big_hero_logo(self):
        self.assertNotIn("lgo-hero", self.html + self.css())
        self.assertIn("lgo-nav", self.html)
        self.assertIn("lgo-foot", self.html)

    def test_r4_tagline_curly_quotes_in_json(self):
        t = CONTENT["hero"]["tagline"]
        self.assertEqual(t, "Tempat dimana para \u201cAku\u201d menCari sang \u201cKamu\u201d.")
        self.assertIn(t, self.html.replace("&#34;", '"'))
        self.assertRegex(self.css(), r"\.tagline\{font:italic[^}]*var\(--serif\)[^}]*letter-spacing")

    def test_r6_trust_strip_after_cta(self):
        self.assertEqual(CONTENT["hero"]["trust"], ["Terverifikasi KTP", "Dikurasi manusia", "Data dijaga privat"])
        h = self.html
        self.assertLess(h.index('class="actions"'), h.index('class="trust"'))
        self.assertEqual(h.split('class="trust"')[1].split("</ul>")[0].count("<svg"), 3)

    def test_r7_nav_cta_opens_register_dialog(self):
        self.assertRegex(self.html, r'<a class="btn btn-primary nav-cta" href="#dlg-daftar" data-dialog="dlg-daftar">Daftar</a>')
        self.assertIn('id="dlg-daftar"', self.html)

    def test_r8_wa_champagne_not_green_same_target(self):
        wa = [l for l in self.css().splitlines() if l.startswith(".wa{")][0]
        self.assertIn("var(--champ)", wa)
        self.assertNotIn("#0f3d2e", self.css())
        self.assertIn('class="wa" aria-label="WhatsApp channel ACK" href="https://whatsapp.com/channel/0029Vb98jsT5vK9ziES81z20"', self.html)
        self.assertRegex(self.css(), r"\.wa\{position:fixed;right:1rem;bottom:1rem;left:auto;width:54px;height:54px;[^}]*border-radius:50%")  # mobile: round bubble, not full-width bar
        self.assertNotIn("padding-bottom:52px", self.css())

    def test_r9_no_aku_kamu_kita_filler(self):
        self.assertNotIn('class="stages"', self.html)
        self.assertNotIn("stages", CONTENT["hero"])
        self.assertNotIn(">Kita<", self.html)

    def test_r10_hero_bottom_blank_reduced(self):
        css = self.css()
        self.assertNotIn("height:118vh", css)
        self.assertIn("height:108vh", css)


if __name__ == "__main__":
    unittest.main()
