# ACK (Aku Cari Kamu) Prototype

Fondasi aplikasi: FastAPI + Jinja2 + SQLite, autentikasi staff (argon2 password hashing, server-side session cookie), multi-role RBAC, CSRF token, dan audit log minimal.
Status saat ini: Stage 4 (staff). Cakupan: antrean verifikasi, kartu Silver + tautan WA, impor/ekspor CSV (format WA Silver), audit berhalaman, retensi KTP (Owner). Jangan menginput data pribadi asli klien.

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

---

## Tahap 2: Pendaftaran member (`/daftar`)

Scope: form 5 langkah, `/akun` (konfirmasi + masuk/keluar minimal), file privat. **Belum** ada: pembayaran, matching, panel staff untuk member (Tahap 3-4), retensi/cron KTP (Tahap 4).

- Tanggal lahir lengkap, umur >= 18 mutlak. Tidak ada kolom/jalur override.
- KTP disensor di browser (canvas); hanya blob `ktp_redacted` yang dikirim, dienkripsi Fernet (`FERNET_KEY` di `.env`) di `data/private/<random>.enc`. Foto wajah/badan: Pillow decode, resize 1600px, EXIF dibuang, JPEG, `data/private/<random>.jpg` (bukan folder publik).
- Akses file: `GET /berkas/<id>` hanya staff `verifier`/`owner`, tiap akses masuk audit log.
- Cookie member `ack_member` (HttpOnly, SameSite=Lax, Secure sesuai `COOKIE_SECURE`) terpisah dari sesi staff. CSRF di semua POST. Rate limit 10 submit/jam/IP; body upload dibatasi pada byte yang benar-benar diterima.
- Program berharga NULL/0 -> langganan `pending` tanpa invoice. Status `Belum Resmi Bercerai` / `Menikah/Tunangan/Pacaran` -> `perlu_review=1`, status tetap `pending`.
- Dependency baru: `pillow==13.0.0` (teruji pada Python 3.14).

### Migrasi, backup, rollback (idempotent, additive)

```bash
venv/bin/python db.py backup data/ack.db data/ack.db.pre-stage2   # online backup (mode 600). Salin juga .env (FERNET_KEY) - tanpa key, KTP terenkripsi tak bisa dibuka
venv/bin/python db.py dryrun data/ack.db                          # migrasi pada salinan; sumber tidak disentuh
venv/bin/pip install -r requirements.txt
sudo -S -p '' systemctl restart ack                                        # init_db menjalankan migrasi (PRAGMA user_version=2) saat start
# rollback: systemctl stop ack; kembalikan commit sebelumnya + data/ack.db.pre-stage2; start ack
```

Tes: `venv/bin/python -m unittest` (242+ tes; discovery otomatis, jalankan hanya di remote). Coverage: `venv/bin/coverage run -m unittest && venv/bin/coverage report -m`.

### Pertanyaan terbuka untuk klien
Teks resmi syarat/privasi (masih DRAF), masa retensi KTP, harga dan durasi program, rekening pembayaran, daftar pilihan agama/pendidikan/sumber final.

### Kompresi foto di browser (Stage 2)

- Foto wajah, badan, dan KTP tersensor (diratakan dulu) diekspor ulang ke JPEG q0.85, sisi terpanjang <=1600 px. Ukuran file hasil harus **< 1.000.000 byte**; jika belum, dimensi diperkecil bertahap. Jika tetap tidak muat (sisi <=500 px), foto **ditolak** dan tidak dikirim.
- Batas unggah: 10 MiB per file asli; body aplikasi 3x10 MiB + 512 KiB = 31.981.568 B (JSON 413); Nginx  = 33.554.432 B (413 HTML).
- Format HEIC/HEIF hanya diterima jika browser bisa mendekodekan (Safari); server tetap menolak HEIC mentah. **Safari/HEIC belum diuji** (tidak ada Safari asli di lingkungan uji).

## Tahap 3: Portal member (`/akun`)

**Scope (sesuai brief):** login HP/email + password (`/akun/masuk`, sudah Tahap 2); timeline Pendaftaran → Pembayaran → Verifikasi → Aktif → Perkenalan;
tagihan (rekening dari `BANK_INFO`, baris dipisah `|`; nominal + kode unik 3 digit; batas waktu; upload bukti; status Menunggu Bayar / Menunggu Dicek / Lunas / Ditolak + alasan);
verifikasi KTP & foto (alasan penolakan + unggah ulang); kartu perkenalan (foto close up lewat route terautentikasi, usia, domisili, pendidikan, intro, alasan cocok, Tertarik/Tidak; kontak HP baru tampil jika KEDUANYA tertarik; slot dinner untuk Platinum);
feedback 1-5 + catatan (hanya tim); edit profil (suku/agama/status berubah -> `perlu_review=1`); permintaan hapus akun (butuh password + centang; hanya antrean `deletion_requests` untuk Owner).

**Kode:** `portal.py` (route), `templates/public/akun.html` + `profil.html`, CSS ditambah di `static/css/daftar.css`. Cookie `ack_member` terpisah dari staf; semua POST wajib CSRF; semua query dibatasi `member_id` sesi (IDOR -> 404).

**Skema v3 (aditif, idempotent):** `payments` +`unique_code,due_at,proof_file_id,reject_reason`; `introductions` +`resp_a,resp_b,match_reason`; tabel baru `dinner_slots`, `feedback`, `deletion_requests`.
Tagihan hanya dibuat jika harga program terisi (NULL = "Info harga via admin", tidak pernah gratis/tagihan karangan). Baris lama tanpa kode unik tetap tampil tanpa kode.

**Batasan (sengaja, tahap 4 yang mengisi):** belum ada UI staf untuk membuat usulan perkenalan, slot dinner, menyetujui/menolak pembayaran & verifikasi, atau memproses hapus akun; kolom/tabel sudah siap. Staf tidak bisa melihat bukti transfer lewat `/berkas` sampai Tahap 4 (FILE_VIEWERS tidak diubah).
Bukti transfer tersimpan terenkripsi (Fernet) di `data/private`; KTP tetap terenkripsi. Foto kandidat hanya via `/akun/foto/<id perkenalan>` (wajah pasangan pada perkenalan milik sendiri yang belum ditolak); foto/KTP sendiri tidak punya route member.

### Pertanyaan terbuka (tidak ditebak)
1. Batas waktu bayar: BELUM ADA sumber resmi. `due_at` = NULL (UI: "belum ditetapkan") kecuali env `ACK_PAY_HOURS` diisi eksplisit setelah disetujui klien.
1b. File privat: dihapus hanya lewat tabel `pending_file_cleanup` (v4, aditif; per member). File baru dicatat sebelum ditulis, catatan dihapus dalam transaksi DB yang sama; file lama dicatat dalam transaksi yang sama dengan pelepasan baris. `portal.reconcile_pending(conn, priv_dir, mid)` (dipanggil di akhir request, maks 20 baris; gagal = tetap di antrean, coba lagi pada panggilan berikutnya). Tanpa cron/daemon: sisa antrean baru diproses saat member itu upload lagi atau reconcile dipanggil manual (`reconcile_pending(conn, priv_dir)` untuk semua).
1c. Kolom `members.occupation` (pekerjaan, opsional; v4) ditampilkan di kartu kandidat. Foto close-up kandidat boleh tampil pada perkenalan `proposed` maupun `mutual` (hanya pihak terkait); kontak HP hanya saat `mutual`. Kolom IG belum ada di skema.
1d. Timeline menampilkan fakta per tahap; tanpa tagihan (harga NULL) tahap Pembayaran = "belum ada tagihan", tahap lain tidak digate.
2. Harga program belum ada -> tagihan belum muncul; kode unik ditambahkan ke harga dasar (penambahan, bukan pengurangan).
3. Jika salah satu pihak memilih "Tidak", pihak lain hanya melihat "menunggu" lalu kartu hilang? Saat ini kartu pihak yang menunggu tetap tampil sebagai "Menunggu", penolak tidak diungkap; kartu penolak disembunyikan. Perlu konfirmasi klien.
4. Definisi "Aktif" = langganan `active` (diatur staf Tahap 4); "Perkenalan" selesai = ada pasangan saling tertarik.
5. Penghapusan data: apa yang dihapus vs. disimpan untuk keperluan hukum/keuangan (retensi) menunggu keputusan Owner/klien.
6. Slot dinner: kapan Platinum boleh memilih (dipakai: setelah saling tertarik); Gold/Private Concierge belum punya hak slot.
7. Zona waktu batas bayar ditampilkan UTC; perlu WIB?
8. Edit nama/tanggal lahir/HP/email lewat admin (tidak ada self-service, mencegah lolos gerbang 18+).

### Backup, rollback, perintah teruji
```
venv/bin/python db.py backup data/ack.db data/ack.db.bak-pre-stage3-XXXX   # online, mode 600
venv/bin/python db.py dryrun data/ack.db                                    # -> user_version = 3 pada salinan
venv/bin/python -W error::ResourceWarning -m unittest -q test_stage0 test_stage1 test_stage2 test_stage3
# rollback: systemctl stop ack; git checkout main; cp data/ack.db.bak-pre-stage3-XXXX data/ack.db; systemctl start ack
```

### Stage 4 (staff)

- Endpoint per peran: finance menyetujui pembayaran (`/keuangan`, approve di `staff.py`); verifier memakai `/verifikasi`; matchmaker `/anggota`, `/perkenalan`; owner `/owner/*` (retensi KTP, program, staff, impor CSV, audit).
- Pipeline retensi KTP: tombol Owner saja, batch 20, usia >30 hari; tanpa cleanup lewat GET dan tanpa cron.
- Audit: halaman 50 baris (`LIMIT 50 OFFSET`).
- Keputusan bisnis: durasi program `NULL` = blok kuota; mutual-once `NULL` = tidak diketahui, `0` = tidak ada.
- Migrasi schema versi 6. Backup sebelum deploy (`data/ack.db.bak-pre-*`, chmod 600); rollback: hentikan service, salin balik backup ke `data/ack.db`. Jangan reset DB secara buta; `uploads/` jangan dihapus.
- Tes: `venv/bin/python -m unittest` di remote (discovery otomatis). Tidak ada pip lokal.
- UI staff: target sentuh >= 44px, teks tampil >= 16px, tanpa overflow di 390px.

### Stage 5 chunk 2: bukti bayar event (dalam pengerjaan)

- Schema v8 (additive): `event_regs.proof_file_id/proof_uploaded_at/reject_reason/paid_at/paid_by`. Bukti = `member_files(kind='event_proof')`, terenkripsi Fernet, pola `pending_file_cleanup` yang sama dengan portal.
- Member pemilik: `/akun/event/{id}` (tagihan memakai `locked_price_idr`, tanpa rekening/jatuh tempo karangan) dan unggah `selected`->`proof_uploaded`. Finance/Owner: `/keuangan/event` (lihat bukti no-store, audit hanya ID, setujui sekali -> `paid`, tolak wajib alasan; kursi tetap `selected`, unggah ulang boleh).
- Keputusan: TIDAK ada refund/pembatalan otomatis. Batal untuk `paid`/`checked_in` diblokir sampai kebijakan refund dikonfigurasi; tinjau manual. QR/tamu = chunk berikutnya.

### Stage 5 selesai (QR, tamu, pairing event)

- Scope: check-in QR dengan fallback manual (jsQR decode teruji di browser headless dengan DUMMY; kamera fisik BELUM diuji). Tamu tanpa langganan; privasi berkas tamu tahan lama lewat antrean `flock`. Pairing event: mutual, terpisah, dan EXEMPT dari kuota. Schema versi 10; 299 tes lulus (`-W error::ResourceWarning`). Seluruh data uji DUMMY.
- Rollback (koreksi): baseline kode `db4bd2fbdfe60c444840b7a95df3c3ad3a85616e` (schema 6) + backup `data/ack.db.bak-pre-stage5-v6` (mode 600). Kode, DB, dan `uploads/` harus dikembalikan terkoordinasi; JANGAN restore DB secara buta setelah ada tulis baru. Release = arsip sumber dari commit Stage 5, bukan backup lama.
