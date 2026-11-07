"""SQLite layer: schema, atomic codes, birth-date/age utilities. Dummy data only."""
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone

ROLES = ("owner", "finance", "verifier", "matchmaker", "event")
ROLE_LABEL = {"owner": "Owner", "finance": "Admin Keuangan", "verifier": "Verifikator",
              "matchmaker": "Matchmaker", "event": "Event"}
MIN_AGE = 18
SCHEMA_VERSION = 6   # PRAGMA user_version; v2 = Stage 2 registration cols; v3 = Stage 3 portal; v4 = pending_file_cleanup; v6 = introductions quota/outcome cols; v5 = Stage 4 staff cols + app_settings (all additive, idempotent)
KTP_RETENTION_DAYS_DEFAULT = 30   # seeded into app_settings; ID-photo retention counted from verification approved_at
PAY_HOURS = int(os.environ["ACK_PAY_HOURS"]) if os.environ.get("ACK_PAY_HOURS", "").isdigit() else None  # invoice deadline in hours; None = not set (no approved source) -> due_at NULL, UI "belum ditetapkan"

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
CREATE TABLE IF NOT EXISTS member_sessions(
  token_hash TEXT PRIMARY KEY,
  member_id INTEGER REFERENCES members(id) ON DELETE CASCADE,  -- NULL = anonymous (CSRF for /daftar, /akun/masuk)
  csrf TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reg_attempts(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, ip TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dinner_slots(
  id INTEGER PRIMARY KEY,
  introduction_id INTEGER NOT NULL REFERENCES introductions(id),
  starts_at TEXT NOT NULL, venue TEXT NOT NULL DEFAULT '',
  picked_by INTEGER REFERENCES members(id));
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY,
  introduction_id INTEGER NOT NULL REFERENCES introductions(id),
  member_id INTEGER NOT NULL REFERENCES members(id),
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(introduction_id, member_id));
CREATE TABLE IF NOT EXISTS deletion_requests(
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','declined')), created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS deletion_open_uq ON deletion_requests(member_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS pending_file_cleanup(   -- durable intent to unlink a private file; row survives until unlink succeeded
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  stored_path TEXT NOT NULL UNIQUE, reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
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


class _Res:
    """Rows are fetched while the lock is held: a cursor read later could be reset by another thread's COMMIT/ROLLBACK."""

    def __init__(self, cur):
        self.rowcount, self.lastrowid, self._rows, self._i = cur.rowcount, cur.lastrowid, cur.fetchall(), 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]

    def fetchall(self):
        r, self._i = self._rows[self._i:], len(self._rows)
        return r

    def __iter__(self):
        return iter(self.fetchall())


class Conn(sqlite3.Connection):
    """One RLock guards every statement and whole tx() blocks, so threads sharing a connection never interleave."""
    lock = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lock = threading.RLock()

    def execute(self, *a):
        with self.lock:
            return _Res(super().execute(*a))

    def executemany(self, *a):
        with self.lock:
            return _Res(super().executemany(*a))


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30, factory=Conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Stage 2: columns added to the Stage 0 tables (old rows keep NULL / defaults). No override columns: age is a hard gate.
MEMBER_COLS = (
    ("email", "TEXT"), ("email_key", "TEXT"), ("phone", "TEXT"), ("password_hash", "TEXT"),
    ("gender", "TEXT"), ("domicile", "TEXT"), ("ethnicity", "TEXT"), ("marga", "TEXT"), ("religion", "TEXT"),
    ("marital_status", "TEXT"), ("education", "TEXT"), ("height_cm", "INTEGER"), ("intro", "TEXT"),
    ("pref_age_min", "INTEGER"), ("pref_age_max", "INTEGER"), ("pref_religion", "TEXT"),
    ("pref_domicile", "TEXT"), ("pref_notes", "TEXT"), ("publish_consent", "TEXT"), ("source", "TEXT"),
    ("perlu_review", "INTEGER NOT NULL DEFAULT 0 CHECK(perlu_review IN (0,1))"), ("consented_at", "TEXT"),
    ("occupation", "TEXT"))
V3_COLS = {"payments": (("unique_code", "INTEGER CHECK(unique_code IS NULL OR unique_code BETWEEN 1 AND 999)"),
                        ("due_at", "TEXT"), ("proof_file_id", "INTEGER"), ("reject_reason", "TEXT")),
           "introductions": (("resp_a", "TEXT CHECK(resp_a IS NULL OR resp_a IN ('tertarik','tidak'))"),
                             ("resp_b", "TEXT CHECK(resp_b IS NULL OR resp_b IN ('tertarik','tidak'))"),
                             ("match_reason", "TEXT"))}
V5_COLS = {"subscriptions": (("starts_at", "TEXT"), ("ends_at", "TEXT")),          # set on payment approval; NULL = not started
           "members": (("pipeline", "TEXT"), ("internal_notes", "TEXT"), ("next_followup_at", "TEXT")),
           "verifications": (("approved_at", "TEXT"), ("checklist", "TEXT"))}      # checklist = JSON text
V6_COLS = {"introductions": (("quota_tracked", "INTEGER NOT NULL DEFAULT 0 CHECK(quota_tracked IN (0,1))"),   # 0 = legacy Stage-3 row, never charged
                             ("quota_consumed", "INTEGER NOT NULL DEFAULT 0 CHECK(quota_consumed IN (0,1))"),
                             ("mutual_at", "TEXT"),
                             ("outcome", "TEXT CHECK(outcome IS NULL OR outcome IN ('dinner_scheduled','berhasil','tidak_lanjut'))"))}
FILE_COLS = (("encrypted", "INTEGER NOT NULL DEFAULT 0 CHECK(encrypted IN (0,1))"),)


def migrate(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    with tx(conn):
        for table, cols in (("members", MEMBER_COLS), ("member_files", FILE_COLS), *V3_COLS.items(), *V5_COLS.items(), *V6_COLS.items()):
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # race-proof uniqueness (NULLs, i.e. pre-Stage-2 rows, never collide)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS members_phone_uq ON members(phone)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS members_email_uq ON members(email_key)")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('ktp_retention_days',?)",
                     (str(KTP_RETENTION_DAYS_DEFAULT),))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO programs(name,description,benefits,sort_order,proposed) VALUES(?,?,?,?,?)", PROGRAMS)
    migrate(conn)


def backup(src, dst):
    """Online, consistent copy (sqlite backup API; safe while the service runs). Result is mode 600."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    os.chmod(dst, 0o600)


def dryrun(src):
    """Run the migration on a throwaway copy; returns the resulting user_version. Source is never written."""
    tmp = tempfile.mkdtemp()
    try:
        cp = os.path.join(tmp, "copy.db")
        backup(src, cp)
        c = connect(cp)
        init_db(c)
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        return v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def tx(conn):
    """BEGIN IMMEDIATE: serialises writers so counters cannot race."""
    with getattr(conn, "lock", None) or nullcontext():   # held for the whole tx: no other thread's statement lands inside it
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


def new_unique_code(conn, amount):
    """Random 1-999, not already used by another open invoice of the same base amount (transfer matching)."""
    used = {r[0] for r in conn.execute("SELECT unique_code FROM payments WHERE amount_idr=? AND status='pending' "
                                       "AND unique_code IS NOT NULL", (amount,))}
    return secrets.choice([c for c in range(1, 1000) if c not in used] or [1])


def due_iso(start=None):
    if PAY_HOURS is None:
        return None
    return ((start or datetime.now(timezone.utc)) + timedelta(hours=PAY_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def next_member_code(conn):
    return f"ACK-{_bump(conn, 'member'):04d}"


def add_member(conn, full_name, birth_str, today=None):
    """Create member with next ACK-NNNN. Validates before touching the counter (no burned numbers)."""
    born = parse_birth_date(birth_str)
    if not is_adult(born, today):
        raise ValueError(f"usia minimal {MIN_AGE} tahun")
    with tx(conn):
        code = next_member_code(conn)
        conn.execute("INSERT INTO members(code,full_name,birth_date,created_at) VALUES(?,?,?,?)",
                     (code, full_name, born.isoformat(), now_iso()))
    return code


def price_label(price):
    # NULL = not configured yet; never render as free.
    if price is None:
        return "Info harga via admin"
    return "Gratis" if price == 0 else "Rp " + f"{price:,}".replace(",", ".")


if __name__ == "__main__":  # python db.py backup SRC DST | python db.py dryrun SRC
    if len(sys.argv) == 4 and sys.argv[1] == "backup":
        backup(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "dryrun":
        print("dryrun ok, user_version =", dryrun(sys.argv[2]))
    else:
        sys.exit("usage: db.py backup SRC DST | db.py dryrun SRC")
