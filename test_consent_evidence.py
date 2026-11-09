"""ACK readiness: minimal consent evidence (server-owned snapshot + digest).
Synthetic data only. Guest and normal registration flows bind GET-displayed legal text
to POST via a signed, stateless binding; legacy rows keep NULL evidence."""
import hashlib
import json
import os
import re
import unittest
from html.parser import HTMLParser

import consent_evidence
import landing
import test_stage2 as t2
import test_stage4_staff as t4
from fastapi.testclient import TestClient

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')

CHECKBOX_KEYS = ("consent_terms", "consent_privacy", "consent_honest",
                 "ktp_nik_ok", "ktp_addr_ok", "ktp_sign_ok")


class CheckboxLabels(HTMLParser):
    """Visible text per <label class="chk"> checkbox, keyed by input name.
    convert_charrefs=True decodes entities; <a> inner text stays (it is visible)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs, self._name, self._buf = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label" and "chk" in (a.get("class") or "").split():
            self._name, self._buf = None, []
        elif tag == "input" and self._buf is not None:
            self._name = a.get("name")

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "label" and self._buf is not None:
            if self._name:
                self.pairs.append((self._name, " ".join("".join(self._buf).split())))
            self._name, self._buf = None, None


def checkbox_labels(html):
    p = CheckboxLabels()
    p.feed(html)
    return dict(p.pairs)


class CheckboxTextSpans(HTMLParser):
    """After each checkbox input: list of immediate-child span texts of the label.
    Flex layout treats each contiguous text run as a separate item; wrapping the
    text in one span keeps the checkbox as the only other flex item, so mobile
    rows stay two columns instead of fragmenting into one column per text run."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.spans, self._in_chk, self._name, self._depth, self._buf = [], False, None, 0, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label" and "chk" in (a.get("class") or "").split():
            self._in_chk, self._name, self._depth, self._buf = True, None, 0, None
        elif self._in_chk and tag == "input":
            self._name = a.get("name")
        elif self._in_chk and self._name and tag == "span" and self._depth == 0:
            self._depth, self._buf = 1, []
        elif self._buf is not None:
            self._depth += 1

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._in_chk and tag == "label":
            self._in_chk, self._name, self._depth, self._buf = False, None, 0, None
        elif self._buf is not None:
            if tag == "span" and self._depth == 1:
                self.spans.append((self._name, " ".join("".join(self._buf).split())))
                self._depth, self._buf = 0, None
            else:
                self._depth -= 1


def checkbox_text_spans(html):
    p = CheckboxTextSpans()
    p.feed(html)
    grouped = {}
    for name, text in p.spans:
        grouped.setdefault(name, []).append(text)
    return grouped


def canonical_legal(flow):
    legal = landing.load_content()["legal"]
    boxes = (consent_evidence.NORMAL_CONSENT_BOXES if flow == "normal"
             else consent_evidence.CONSENT_BOXES)
    return {"flow": flow, "terms": legal["terms"], "privacy": legal["privacy"],
            "checkboxes": boxes, "ktp": consent_evidence.KTP_BOXES}


def expected_digest(flow):
    snap = canonical_legal(flow)
    blob = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return snap, hashlib.sha256(blob).hexdigest()


class ConsentEvidence(t4.S):
    def setUp(self):
        super().setUp()
        self.w.execute("INSERT INTO events(title,slug,starts_at,capacity,quota_m,quota_f,price_idr,active,created_at,extra_fields) "
                       "VALUES('Dinner','dinner-demo','2026-12-01T12:00',2,1,1,100000,1,'t','[]')")

    def gfiles(self):
        return {"ktp_redacted": ("k.jpg", t2.img_bytes(), "image/jpeg"),
                "photo_face": ("f.jpg", t2.img_bytes(exif=True), "image/jpeg")}

    def gf(self, **o):
        d = dict(full_name="Tamu Dummy", phone="081299990001", email="tamu1@example.test",
                 birth_date="1995-05-17", gender="Perempuan", password="dummy-Passw0rd-123",
                 password2="dummy-Passw0rd-123", slug="dinner-demo",
                 ktp_nik_ok="on", ktp_addr_ok="on", ktp_sign_ok="on",
                 consent_terms="on", consent_privacy="on", consent_honest="on")
        d.update(o)
        return {k: v for k, v in d.items() if v is not None}

    def gpost(self, f=None, fl=None, csrf=None, binding=None):
        r = self.c.get("/event/dinner-demo/tamu")
        self.assertEqual(r.status_code, 200)
        if csrf is None:
            csrf = CSRF_RE.search(r.text).group(1)
        if binding is None and f is None:
            m = re.search(r'name="consent_binding" value="([^" ]+)', r.text)
            binding = m.group(1) if m else None
        data = self.gf() if f is None else dict(f)
        if binding is not None and "consent_binding" not in data:
            data["consent_binding"] = binding
        return self.c.post("/event/tamu/submit", data=data,
                           files=self.gfiles() if fl is None else fl,
                           headers={"X-CSRF-Token": csrf})

    def legal_of(self, html):
        # exact displayed titles/paragraphs from the rendered page
        out = {}
        for did, key in (("syarat", "terms"), ("privasi", "privacy")):
            m = re.search(r'<details id="' + did + r'"><summary>(.*?)</summary>(.*?)</details>', html, re.S)
            self.assertTrue(m, did)
            title = m.group(1)
            paras = re.findall(r"<p>(.*?)</p>", m.group(2), re.S)
            out[key] = {"title": title, "paragraphs": paras}
        return out

    def test_guest_get_displays_full_draft_legal_like_normal(self):
        rg = self.c.get("/event/dinner-demo/tamu")
        rn = self.c.get("/daftar")
        self.assertEqual((rg.status_code, rn.status_code), (200, 200))
        g, n = self.legal_of(rg.text), self.legal_of(rn.text)
        self.assertEqual(g, n)
        src = landing.load_content()["legal"]
        self.assertEqual(g["terms"]["title"], src["terms"]["title"])
        self.assertEqual(g["privacy"]["paragraphs"], src["privacy"]["paragraphs"])

    def test_get_renders_exact_checkbox_labels_guest_flow(self):
        html = self.c.get("/event/dinner-demo/tamu").text
        shown = checkbox_labels(html)
        self.assertEqual([shown[k] for k in CHECKBOX_KEYS[:3]],
                         [c["label"] for c in consent_evidence.CONSENT_BOXES])
        self.assertEqual([shown[k] for k in CHECKBOX_KEYS[3:]],
                         [c["label"] for c in consent_evidence.KTP_BOXES])

    def test_get_renders_exact_checkbox_labels_normal_flow(self):
        html = self.c.get("/daftar").text
        shown = checkbox_labels(html)
        self.assertEqual([shown[k] for k in CHECKBOX_KEYS[:3]],
                         [c["label"] for c in consent_evidence.NORMAL_CONSENT_BOXES])
        self.assertEqual([shown[k] for k in CHECKBOX_KEYS[3:]],
                         [c["label"] for c in consent_evidence.KTP_BOXES])

    def test_get_wraps_consent_text_in_single_span_per_checkbox(self):
        html = self.c.get("/daftar").text
        spans = checkbox_text_spans(html)
        labels = checkbox_labels(html)
        for key in CHECKBOX_KEYS[:2]:
            span_texts = spans[key]
            self.assertEqual(len(span_texts), 1, (key, span_texts))
            self.assertEqual(span_texts[0], labels[key])

    def test_guest_get_and_normal_get_render_signed_binding(self):
        for path, flow in (("/event/dinner-demo/tamu", "guest"), ("/daftar", "normal")):
            html = self.c.get(path).text
            m = re.search(r'name="consent_binding" value="([^" ]+)', html)
            self.assertTrue(m, path)
            self.assertGreater(len(m.group(1)), 30)

    def test_guest_happy_persists_exact_snapshot_and_digest(self):
        r0 = self.c.get("/event/dinner-demo/tamu")
        shown = self.legal_of(r0.text)
        _, dig = expected_digest("guest")
        r = self.gpost(csrf=CSRF_RE.search(r0.text).group(1))
        self.assertEqual(r.status_code, 200, r.text)
        row = self.w.execute("SELECT consent_text,consent_digest,consented_at FROM members").fetchone()
        snap = json.loads(row["consent_text"])
        self.assertEqual(snap["terms"], shown["terms"])
        self.assertEqual(snap["privacy"], shown["privacy"])
        self.assertEqual(snap["flow"], "guest")
        shown_labels = checkbox_labels(r0.text)
        self.assertEqual([c["label"] for c in snap["checkboxes"]],
                         [shown_labels[k] for k in CHECKBOX_KEYS[:3]])
        self.assertEqual([c["label"] for c in snap["ktp"]],
                         [shown_labels[k] for k in CHECKBOX_KEYS[3:]])
        self.assertEqual(row["consent_digest"], dig)
        self.assertTrue(row["consented_at"])

    def test_guest_unchecked_consent_rejects_before_any_writes(self):
        csrf = CSRF_RE.search(self.c.get("/event/dinner-demo/tamu").text).group(1)
        r = self.gpost(f=self.gf(consent_honest=None), csrf=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)

    def test_guest_missing_binding_rejected_no_rows_no_files(self):
        csrf = CSRF_RE.search(self.c.get("/event/dinner-demo/tamu").text).group(1)
        r = self.gpost(f=self.gf(consent_binding=None), csrf=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM event_regs").fetchone()[0], 0)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM member_files").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.priv), [])

    def test_guest_tampered_binding_rejected_no_rows_no_files(self):
        csrf = CSRF_RE.search(self.c.get("/event/dinner-demo/tamu").text).group(1)
        binding = re.search(r'name="consent_binding" value="([^" ]+)',
                            self.c.get("/event/dinner-demo/tamu").text).group(1)
        bad = binding[:-4] + ("AAAA" if not binding.endswith("AAAA") else "BBBB")
        r = self.gpost(f=self.gf(consent_binding=bad), csrf=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.priv), [])

    def test_guest_stale_binding_after_content_change_rejected(self):
        r0 = self.c.get("/event/dinner-demo/tamu")
        csrf = CSRF_RE.search(r0.text).group(1)
        binding = re.search(r'name="consent_binding" value="([^" ]+)', r0.text).group(1)
        # rotate the displayed content by editing the file on disk, then restore
        import landing
        path = landing.CONTENT_PATH
        with open(path, encoding="utf-8") as fh:
            orig = fh.read()
        new = orig.replace('"Kebijakan privasi — DRAF"', '"Kebijakan privasi — DRAF v2"')
        def restore(p=path, o=orig):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(o)
        self.addCleanup(restore)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
        r = self.gpost(f=self.gf(consent_binding=binding), csrf=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.priv), [])

    def test_cross_flow_binding_rejected(self):
        nb = re.search(r'name="consent_binding" value="([^" ]+)', self.c.get("/daftar").text).group(1)
        csrf = CSRF_RE.search(self.c.get("/event/dinner-demo/tamu").text).group(1)
        r = self.gpost(f=self.gf(consent_binding=nb), csrf=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)

    def test_normal_happy_persists_exact_snapshot_and_digest(self):
        r0 = self.c.get("/daftar")
        shown = self.legal_of(r0.text)
        csrf = CSRF_RE.search(r0.text).group(1)
        binding = re.search(r'name="consent_binding" value="([^" ]+)', r0.text).group(1)
        _, dig = expected_digest("normal")
        r = self.c.post("/daftar/submit", data=t2.fields(consent_binding=binding),
                        files=t2.files(), headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.w.execute("SELECT consent_text,consent_digest,consented_at FROM members").fetchone()
        snap = json.loads(row["consent_text"])
        self.assertEqual(snap["terms"], shown["terms"])
        self.assertEqual(snap["privacy"], shown["privacy"])
        self.assertEqual(snap["flow"], "normal")
        shown_labels = checkbox_labels(r0.text)
        self.assertEqual([c["label"] for c in snap["checkboxes"]],
                         [shown_labels[k] for k in CHECKBOX_KEYS[:3]])
        self.assertEqual([c["label"] for c in snap["ktp"]],
                         [shown_labels[k] for k in CHECKBOX_KEYS[3:]])
        self.assertEqual(row["consent_digest"], dig)
        self.assertTrue(row["consented_at"])

    def test_normal_missing_binding_rejected(self):
        csrf = CSRF_RE.search(self.c.get("/daftar").text).group(1)
        r = self.c.post("/daftar/submit", data=t2.fields(), files=t2.files(),
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.w.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)

    def test_no_timestamp_in_digest(self):
        snap, dig = expected_digest("guest")
        blob = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertNotIn("consented_at", blob.decode())
        self.assertNotIn("timestamp", blob.decode())

    def test_migration_twice_preserves_legacy_nulls(self):
        import db
        conn = db.connect(self.dbp)
        self.addCleanup(conn.close)
        self.w.execute("INSERT INTO members(code,full_name,birth_date,status,created_at) "
                       "VALUES('ACK-0001','Legacy Row','1990-01-01','draft','t')")
        db.migrate(conn)
        db.migrate(conn)
        row = self.q.execute("SELECT consent_text,consent_digest,consented_at FROM members "
                             "WHERE code='ACK-0001'").fetchone()
        self.assertIsNone(row["consent_text"])
        self.assertIsNone(row["consent_digest"])
        self.assertIsNone(row["consented_at"])
        self.assertEqual(self.q.execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
