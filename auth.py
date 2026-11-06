"""Staff auth: argon2 hashing, server-side sessions, bounded login rate limit."""
import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

import db

COOKIE = "ack_session"
TTL = 8 * 3600
ANON_TTL = 3600
MAX_USER_FAILS = 5
MAX_IP_FAILS = 20
WINDOW = 900
ANON_CAP = 2000

_ph = PasswordHasher()
DUMMY_HASH = _ph.hash("timing-equaliser-not-a-password")


def hash_password(pw):
    return _ph.hash(pw)


def verify_password(h, pw):
    try:
        return _ph.verify(h, pw)
    except (VerificationError, InvalidHashError):
        return False


def _th(secret, tok):
    return hmac.new(secret.encode(), tok.encode(), hashlib.sha256).hexdigest()


def create_session(conn, secret, staff_id=None):
    """Always a fresh random token (rotation). Only the keyed hash is stored."""
    now = int(time.time())
    ttl = TTL if staff_id else ANON_TTL
    tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
    if staff_id is None:
        n = conn.execute("SELECT COUNT(*) FROM sessions WHERE staff_id IS NULL").fetchone()[0]
        if n >= ANON_CAP:
            conn.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions "
                         "WHERE staff_id IS NULL ORDER BY created_at LIMIT ?)", (ANON_CAP // 2,))
    conn.execute("INSERT INTO sessions(token_hash,staff_id,csrf,created_at,expires_at) VALUES(?,?,?,?,?)",
                 (_th(secret, tok), staff_id, csrf, now, now + ttl))
    return tok, csrf, ttl


def load_session(conn, secret, tok):
    th = _th(secret, tok)
    r = conn.execute(
        "SELECT s.staff_id,s.csrf,s.expires_at,u.username,u.display_name,u.active FROM sessions s "
        "LEFT JOIN users_staff u ON u.id=s.staff_id WHERE s.token_hash=?", (th,)).fetchone()
    if not r:
        return None
    if r["expires_at"] < time.time() or (r["staff_id"] is not None and not r["active"]):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        return None
    roles = {x[0] for x in conn.execute("SELECT role FROM staff_roles WHERE staff_id=?", (r["staff_id"],))}
    return {"token_hash": th, "staff_id": r["staff_id"], "csrf": r["csrf"], "username": r["username"],
            "display_name": r["display_name"], "roles": roles}


def drop_session(conn, th):
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))


def csrf_ok(sess, token):
    return bool(sess) and hmac.compare_digest(sess["csrf"], token or "")


# ponytail: counts failures per username and per IP in a sliding window; table is purged on every
# check so it stays bounded by IP budget. Add a global cap / reverse-proxy IP handling if exposed wider.
def is_locked(conn, user, ip):
    conn.execute("DELETE FROM login_attempts WHERE ts<?", (int(time.time()) - WINDOW,))
    u = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username=?", (user,)).fetchone()[0]
    i = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0]
    return u >= MAX_USER_FAILS or i >= MAX_IP_FAILS


def record_fail(conn, user, ip):
    conn.execute("INSERT INTO login_attempts(ts,username,ip) VALUES(?,?,?)", (int(time.time()), user, ip))


def clear_fails(conn, user):
    conn.execute("DELETE FROM login_attempts WHERE username=?", (user,))
