# ACK (Aku Cari Kamu) Prototype

Fondasi aplikasi: FastAPI + Jinja2 + SQLite, autentikasi staff (argon2 password hashing, server-side session cookie), multi-role RBAC, CSRF token, dan audit log minimal.
Status saat ini: Prototype Tahap 1 (Landing page responsif + Fondasi Tahap 0). Jangan menginput data pribadi asli klien.

---

## Repository & Development Policy

- **Repository Canonical**: `https://github.com/jasmiandre/ack-zen`
- **Git Remote (SSH)**: `git@github.com:jasmiandre/ack-zen.git`
- **Kebijakan Edit Server**: Pengembangan dan revisi aktif tetap dilanjutkan oleh Zen langsung di server (`recloud-dev`) seperti biasa. GitHub berfungsi sebagai repositori backup, tracking riwayat revisi, dan snapshot berkala tiap stage.
- **Dedicated Deploy Key (Server)**:
  - Key path: `/home/zen/.ssh/id_ed25519_ack_zen`
  - Repo-local SSH Command: `git config core.sshCommand "ssh -i /home/zen/.ssh/id_ed25519_ack_zen -o IdentitiesOnly=yes"`

---

## Panduan Menjalankan

### A. Linux / Server (`recloud-dev`)

```bash
# 1. Virtual Environment & Dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Inisialisasi Database & Secret (pertama kali saja)
venv/bin/python seed.py
# seed.py otomatis membuat .env (chmod 600) dan seed_credentials.txt (chmod 600) jika belum ada.

# 3. Jalankan Server
# Melalui systemd (recloud-dev):
sudo cp infra/ack.service /etc/systemd/system/ack.service
sudo systemctl daemon-reload
sudo systemctl enable --now ack.service

# Atau manual:
./run.sh
# HOST=127.0.0.1 PORT=8088 COOKIE_SECURE=false venv/bin/python -c "import uvicorn, os; uvicorn.run('app:app', host=os.getenv('HOST','127.0.0.1'), port=int(os.getenv('PORT','8088')))"

# 4. Menjalankan Test Suite
venv/bin/python -m unittest -v test_stage0
venv/bin/python -m unittest -v test_stage1
venv/bin/coverage run -m unittest discover -p "test_*.py" && venv/bin/coverage report -m
```

### B. Windows Local Development (PowerShell)
> **PENTING / PERHATIAN**: Instruksi Windows ini belum diuji secara langsung di lingkungan Windows (*UNTESTED*). Disiapkan sebagai panduan standar environment lokal.

```powershell
# 1. Pastikan Python 3.10+ terpasang, lalu buat virtual environment
py -3.10 -m venv venv
venv\Scripts\python -m pip install -r requirements.txt

# 2. Inisialisasi Environment & Database Lokal
# PERINGATAN KEAMANAN:
# - JANGAN MENYALIN FILE .env ATAU SECRET DARI SERVER KE KOMPUTER LOKAL!
# - JANGAN MENCETAK / MEMBAGIKAN ISI SECRET DI CHAT / SCREENSHOT!
venv\Scripts\python seed.py

# Catatan Hak Akses File (Windows ACL):
# Di Linux, seed.py otomatis menerapkan chmod 600. Di Windows, batasi hak akses .env menggunakan icacls:
# icacls .env /inheritance:r /grant:r "%USERNAME%:F"

# 3. Menjalankan Server Lokal (LOCAL_ONLY)
$env:HOST="127.0.0.1"
$env:PORT="8088"
$env:COOKIE_SECURE="false"
venv\Scripts\python -c "import os, uvicorn; uvicorn.run('app:app', host=os.getenv('HOST','127.0.0.1'), port=int(os.getenv('PORT','8088')))"

# 4. Menjalankan Test Suite di Windows
venv\Scripts\python -m unittest -v test_stage0
venv\Scripts\python -m unittest -v test_stage1
```

---

## Manajemen Rahasia & Kredensial (Tidak Pernah Dicetak / Di-log)

- `.env` (`chmod 600`): Menyimpan `SECRET_KEY`, `FERNET_KEY`, `OWNER_USER`, `OWNER_PASS`, `BANK_INFO`, `WA_ADMIN`, dan `COOKIE_SECURE`. Digenerate sekali saat seed pertama dan tidak pernah ditimpa.
- `seed_credentials.txt` (`chmod 600`): Menyimpan password akun demo staff (`demo_finance`, `demo_verifier`, `demo_matchmaker`, `demo_event`). Password `owner` hanya berada di `.env`.
- `COOKIE_SECURE=false`: Digunakan pada lingkungan demo HTTP lokal/internal (banner peringatan akan tampil). Wajib diatur `true` jika diakses via HTTPS produksi.

---

## Lingkup & Batasan Arsitektur

- **Single-worker Only**: Menggunakan satu proses Uvicorn dengan satu koneksi SQLite bersama yang dikelola rapi melalui event `lifespan` saat shutdown. Dilarang menggunakan `--workers N` karena transaksi SQLite dilakukan secara in-process.
- **Validasi Input**: Password dibatasi maksimal 256 karakter (ditolak dengan 401 generik jika melebihi, tidak dipotong).
- **Role RBAC**: `owner` (semua hak akses + manajemen staff + audit log), `finance`, `verifier`, `matchmaker`, `event`. Satu user staff dapat memiliki multiple roles. Pengecekan role dilakukan di server (`/access/{role}`, `/owner/*`).

---

## Backup & Pemulihan Data

- **Database SQLite**: Jangan menggunakan `cp` biasa saat server sedang aktif menulis. Gunakan SQLite Online Backup API:
  ```bash
  (umask 077; venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/ack.db'); d=sqlite3.connect('backup.db'); s.backup(d); d.close()"); chmod 600 backup.db
  ```
- **FERNET_KEY**: Kunci untuk enkripsi data sensitif (misalnya KTP). Simpan salinan terenkripsi (misal via GPG) di lokasi aman di luar server dan terpisah dari backup database. Jangan pernah memasukkan kunci ini ke repositori Git.

---

## Aturan Format Data

- Tanggal lahir wajib lengkap format `YYYY-MM-DD` (divalidasi dengan `CHECK` constraint di DB dan pengecekan umur >= 18 tahun pada `db.add_member`).
- `programs.price_idr`: Jika bernilai `NULL`, berarti belum ditetapkan harga ("Info harga via admin"), BUKAN gratis.
- Format penomoran otomatis: `ACK-0001` (counter atomik dengan `BEGIN IMMEDIATE` + `UNIQUE`), `INV-YYMM-0001` (counter invoice bulanan).

---

## Rollback & Penghentian Layanan (systemd / Tahap 0)

```bash
# Penghentian / status layanan systemd:
sudo systemctl status ack.service
sudo systemctl restart ack.service
sudo systemctl stop ack.service

# Rollback ke screen manual (jika diperlukan):
sudo systemctl disable --now ack.service
# Pastikan port 8088 bebas sebelum start manual
ss -tulpn | grep 8088
screen -dmS ack-stage0 ./run.sh

# Data (data/ack.db, .env) tetap DISIMPAN; tidak ada file data yang dihapus.
# Menutup port publik jika diperlukan:
sudo ufw delete allow 8088/tcp
```

---

## Brand Concept & Aset Desain

- Logo yang tampil di template halaman merupakan adaptasi SVG buatan sendiri (`templates/public/_logo.html`, `static/img/favicon.svg`), terinspirasi dari logo asli berupa simbol dua hati outline saling cermin dan wordmark serif bernuansa dusty rose.
- File logo bitmap resmi tersimpan di direktori aset (`static/img/logo-ack*.png`) dan dipertahankan dalam Git tracking untuk kebutuhan brand identitas.
