"""First-run bootstrap: .env (generated once, mode 600), DB, one staff per role. Never prints secrets."""
import os
import secrets

from cryptography.fernet import Fernet

import auth
import db

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
DB_PATH = os.path.join(HERE, "data", "ack.db")
CRED_PATH = os.path.join(HERE, "seed_credentials.txt")
SEED_ROLES = ("finance", "verifier", "matchmaker", "event")


def _write_private(path, text, flags):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | flags, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def init_env(path):
    """Create .env once; returns False (and touches nothing) if it exists."""
    if os.path.exists(path):
        return False
    lines = [
        "HOST=0.0.0.0", "PORT=8088",
        f"SECRET_KEY={secrets.token_urlsafe(48)}",
        f"FERNET_KEY={Fernet.generate_key().decode()}",
        "OWNER_USER=owner", f"OWNER_PASS={secrets.token_urlsafe(18)}",
        "BANK_INFO=", "WA_ADMIN=",
        "COOKIE_SECURE=false",
    ]
    _write_private(path, "\n".join(lines) + "\n", os.O_EXCL)
    return True


def read_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v
    return env


def seed_staff(conn, owner_user, owner_pass, cred_path):
    """Idempotent. Owner password lives only in .env; other roles' passwords go to cred_path (600)."""
    new_lines, created = [], 0
    plan = [(owner_user, "Owner (dummy)", owner_pass, "owner")]
    plan += [(f"demo_{r}", f"Demo {db.ROLE_LABEL[r]}", secrets.token_urlsafe(14), r) for r in SEED_ROLES]
    for user, name, pw, role in plan:
        if conn.execute("SELECT 1 FROM users_staff WHERE username=?", (user,)).fetchone():
            continue
        h = auth.hash_password(pw)
        with db.tx(conn):
            cur = conn.execute("INSERT INTO users_staff(username,display_name,password_hash,created_at) VALUES(?,?,?,?)",
                               (user, name, h, db.now_iso()))
            conn.execute("INSERT INTO staff_roles(staff_id,role) VALUES(?,?)", (cur.lastrowid, role))
            db.audit(conn, cur.lastrowid, "seed_staff", role)
        created += 1
        if role != "owner":
            new_lines.append(f"{user}:{pw}\n")
    if new_lines:
        first = not os.path.exists(cred_path)
        _write_private(cred_path, ("# DUMMY seed staff credentials, mode 600, do not commit\n" if first else "")
                       + "".join(new_lines), os.O_APPEND)
    return created


def main():
    made = init_env(ENV_PATH)
    env = read_env(ENV_PATH)
    os.makedirs(os.path.dirname(DB_PATH), mode=0o700, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    n = seed_staff(conn, env["OWNER_USER"], env["OWNER_PASS"], CRED_PATH)
    print(f"seed: .env {'dibuat' if made else 'sudah ada'}, staff baru: {n}")


if __name__ == "__main__":
    main()
