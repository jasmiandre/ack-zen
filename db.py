"""SQLite layer: schema, atomic codes, birth-date/age utilities. Dummy data only."""
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

ROLES = ("owner", "finance", "verifier", "matchmaker", "event")
ROLE_LABEL = {"owner": "Owner", "finance": "Admin Keuangan", "verifier": "Verifikator",
              "matchmaker": "Matchmaker", "event": "Event"}
MIN_AGE = 18

SCHEMA = """
CREATE TABLE IF NOT EXISTS users_staff(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS staff_roles(
  staff_id INTEGER NOT NULL REFERENCES users_staff(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('owner','finance','verifier','matchmaker','event')),
  PRIMARY KEY(staff_id, role));
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  staff_id INTEGER REFERENCES users_staff(id) ON DELETE CASCADE,  -- NULL = anonymous (login CSRF only)
  csrf TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, username TEXT NOT NULL, ip TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE CHECK(code GLOB 'ACK-[0-9][0-9][0-9][0-9]*'),
  full_name TEXT NOT NULL,
  birth_date TEXT NOT NULL CHECK(birth_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                 AND date(birth_date)=birth_date),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','verified','rejected')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS member_files(
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  kind TEXT NOT NULL, stored_path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS programs(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  benefits TEXT NOT NULL DEFAULT '',
  duration_days INTEGER CHECK(duration_days IS NULL OR duration_days>0),  -- NULL = unknown
  intro_quota INTEGER CHECK(intro_quota IS NULL OR intro_quota>=0),       -- NULL = unknown
  price_idr INTEGER CHECK(price_idr IS NULL OR price_idr>=0),             -- NULL = unconfigured, NOT free
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  proposed INTEGER NOT NULL DEFAULT 0 CHECK(proposed IN (0,1)));
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  program_id INTEGER NOT NULL REFERENCES programs(id),
  status TEXT NOT NULL CHECK(status IN ('pending','active','expired','cancelled')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY,
  invoice_code TEXT NOT NULL UNIQUE CHECK(invoice_code GLOB 'INV-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'),
  subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
  amount_idr INTEGER CHECK(amount_idr IS NULL OR amount_idr>=0),
  status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  reviewer_id INTEGER REFERENCES users_staff(id), note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS introductions(
  id INTEGER PRIMARY KEY,
  member_a INTEGER NOT NULL REFERENCES members(id),
  member_b INTEGER NOT NULL REFERENCES members(id),
  status TEXT NOT NULL DEFAULT 'proposed',
  proposed_by INTEGER REFERENCES users_staff(id), created_at TEXT NOT NULL,
  CHECK(member_a<>member_b));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT,
  capacity INTEGER CHECK(capacity IS NULL OR capacity>=0),
  price_idr INTEGER CHECK(price_idr IS NULL OR price_idr>=0),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_regs(
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  member_id INTEGER NOT NULL REFERENCES members(id),
  status TEXT NOT NULL DEFAULT 'registered', created_at TEXT NOT NULL,
  UNIQUE(event_id, member_id));
CREATE TABLE IF NOT EXISTS wa_templates(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  staff_id INTEGER REFERENCES users_staff(id), action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""

# name, description, benefits, sort, proposed (from the brief; price/duration/quota unknown -> NULL)
PROGRAMS = [
    ("ACK Komunitas", "Masuk grup single terkurasi.", "Grup single terkurasi\nInfo event lebih dulu", 1, 0),
    ("ACK Bronze", "Dikenalkan ke calon pasangan.", "Perkenalan ke calon pasangan\nTukar kontak", 2, 0),
    ("ACK Silver", "Profil diiklankan di kanal ACK (dengan izin).", "Profil di IG/TikTok/WAG ACK\nWajah boleh diblur", 3, 0),
    ("ACK Gold", "Paket gabungan. (USULAN)", "Beberapa kali perkenalan\nProfil di sosmed\nPrioritas event", 4, 1),
    ("ACK Platinum / Exclusive", "Dipertemukan makan berdua.", "Tempat dan jadwal diatur tim", 5, 0),
    ("ACK Private Concierge", "Matchmaker pribadi, profil tidak dipublikasikan. (USULAN)",
     "Konsultasi 1-on-1\nKurasi paling ketat\nProfil tidak dipublikasikan", 6, 1),
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO programs(name,description,benefits,sort_order,proposed) VALUES(?,?,?,?,?)", PROGRAMS)


@contextmanager
def tx(conn):
    """BEGIN IMMEDIATE: serialises writers so counters cannot race."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _bump(conn, name):
    if not conn.in_transaction:
        raise RuntimeError("counter bump must run inside db.tx()")
    return conn.execute(
        "INSERT INTO counters(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1 RETURNING value",
        (name,)).fetchone()[0]


def next_invoice_code(conn, d):
    return f"INV-{d:%y%m}-{_bump(conn, 'inv-' + d.strftime('%y%m')):04d}"


def audit(conn, staff_id, action, detail=""):
    conn.execute("INSERT INTO audit_log(ts,staff_id,action,detail) VALUES(?,?,?,?)",
                 (now_iso(), staff_id, action, detail))


# --- birth date / age -------------------------------------------------------
def parse_birth_date(s):
    """Full YYYY-MM-DD only; a bare year or loose format is rejected."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError("tanggal lahir harus lengkap YYYY-MM-DD")
    return date.fromisoformat(s)


def age_on(born, today):
    if born > today:
        raise ValueError("tanggal lahir di masa depan")
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def is_adult(born, today=None):
    return age_on(born, today or date.today()) >= MIN_AGE


def add_member(conn, full_name, birth_str, today=None):
    """Create member with next ACK-NNNN. Validates before touching the counter (no burned numbers)."""
    born = parse_birth_date(birth_str)
    if not is_adult(born, today):
        raise ValueError(f"usia minimal {MIN_AGE} tahun")
    with tx(conn):
        code = f"ACK-{_bump(conn, 'member'):04d}"
        conn.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES(?,?,?,?)",
                     (code, full_name, born.isoformat(), now_iso()))
    return code


def price_label(price):
    # NULL = not configured yet; never render as free.
    if price is None:
        return "Info harga via admin"
    return "Gratis" if price == 0 else "Rp " + f"{price:,}".replace(",", ".")
