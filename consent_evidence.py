"""ACK readiness: consent evidence helpers (server-owned canonical snapshot + digest).
Server-side only: the client never supplies legal text, labels, or digests.
A stateless signed binding ties the GET-displayed draft text to the POST that accepts it.
"""
import hashlib
import hmac
import json

import landing

# Server-known checkbox labels, exactly as rendered by the templates.
# ponytail: plain tuples; a policy catalog table only pays off when drafts get versioned.
CONSENT_BOXES = ({"key": "consent_terms", "label": "Setuju syarat & ketentuan"},
                 {"key": "consent_privacy", "label": "Setuju kebijakan privasi"},
                 {"key": "consent_honest", "label": "Data yang saya isi benar"})
NORMAL_CONSENT_BOXES = (
    {"key": "consent_terms", "label": "Saya menyetujui syarat & ketentuan (draf)"},
    {"key": "consent_privacy", "label": "Saya menyetujui kebijakan privasi (draf)"},
    {"key": "consent_honest", "label": "Saya menyatakan data dan status saya jujur dan benar"},
)
KTP_BOXES = ({"key": "ktp_nik_ok", "label": "NIK sudah tertutup"},
             {"key": "ktp_addr_ok", "label": "Alamat sudah tertutup"},
             {"key": "ktp_sign_ok", "label": "Tanda tangan sudah tertutup"})


def canonical_snapshot(flow, legal=None):
    """Deterministic dict of everything the member consented to: displayed draft
    titles/paragraphs + exact checkbox statements (+ KTP affirmations). No timestamps:
    consented_at is stored separately on members."""
    legal = legal or landing.load_content()["legal"]
    return {"flow": flow, "terms": legal["terms"], "privacy": legal["privacy"],
            "checkboxes": list(NORMAL_CONSENT_BOXES if flow == "normal" else CONSENT_BOXES),
            "ktp": list(KTP_BOXES)}


def digest(snapshot):
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def sign(snapshot, secret_key):
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(secret_key.encode(), payload, hashlib.sha256).hexdigest()
    return mac + "." + payload.hex()


def verify_binding(raw, flow, secret_key):
    """Return the verified snapshot or None. Rejects absent/tampered/other-flow
    bindings before any DB write or file write; content freshness is checked by the
    caller re-computing the digest against current content."""
    if not isinstance(raw, str) or "." not in raw:
        return None
    mac, hex_payload = raw.split(".", 1)
    try:
        payload = bytes.fromhex(hex_payload)
    except ValueError:
        return None
    good = hmac.new(secret_key.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        return None
    try:
        snap = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(snap, dict) or snap.get("flow") != flow:
        return None
    return snap


def binding_matches_content(snapshot):
    """The signed snapshot must equal what the server would render right now
    (stale-form detection) and its digest must match the recorded evidence digest."""
    if snapshot.get("terms") != canonical_snapshot(snapshot.get("flow"))["terms"] or \
       snapshot.get("privacy") != canonical_snapshot(snapshot.get("flow"))["privacy"]:
        return False
    return digest(snapshot) == digest(canonical_snapshot(snapshot.get("flow")))
