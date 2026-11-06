#!/bin/sh
# ACK Stage 0 launcher: seeds .env/DB once, then serves on HOST:PORT from .env (default 0.0.0.0:8088).
# .env is parsed by seed.read_env (never sourced by the shell).
set -eu
cd "$(dirname "$0")"
umask 077
venv/bin/python seed.py
exec venv/bin/python -c '
import seed, uvicorn
e = seed.read_env(seed.ENV_PATH)
uvicorn.run("app:create_app", factory=True, host=e.get("HOST") or "0.0.0.0",
            port=int(e.get("PORT") or 8088), access_log=False, server_header=False)
'
