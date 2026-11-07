# NOTES ACK

## Scope
Hanya TAHAP 0 (override user): fondasi + auth + role. Tanpa landing/member/upload/pembayaran/event. Data dummy saja.
Tidak mengakses domain/sosmed/logo/telepon ACK asli. BANK_INFO & WA_ADMIN kosong.

## Keputusan
- Tanggal lahir lengkap + usia>=18 (override brief).
- Harga NULL = belum diatur.
- Tabel tambahan minimal: staff_roles, sessions, counters, login_attempts.
- Cookie `ack_session` HttpOnly, SameSite=Lax, Secure via COOKIE_SECURE (false pada demo HTTP, ada banner).
- Token sesi disimpan sebagai HMAC hash; rotasi saat login; revokasi server saat logout/disable.
- Rate limit login: 5 gagal/username & 20/IP per 15 menit, tabel dibersihkan tiap cek.
- pip tidak ada di host (ensurepip hilang): venv --without-pip + get-pip.py di venv saja.

- Final scope patch: run.sh tidak men-source .env (seed.read_env + uvicorn.run); login >256 karakter ditolak 401 (tidak dipotong); koneksi SQLite ditutup via lifespan; single-worker.
- Anon session eviction (ANON_CAP) dibatasi by design; tidak ada bukti 500. audit_log.staff_id FK sudah ada (db.py) dan seed/app memakai id nyata; tidak ada perubahan.
- Argon2 tidak memotong 72 byte (itu bcrypt); tidak ada perubahan.

## Hasil Tahap 0
Lihat laporan akhir (tes, URL, screen, manifest). Evidence: evidence/.

## Stage 4: verification queue + KTP retention
- /verifikasi (verifier+owner; finance only if also verifier): KTP/face/body side by side; approve needs 4 checks, reject needs reason. approved_at = review time. Payments/subscriptions untouched. KTP opens audited as member+file IDs.
- /owner/ktp-retensi (Owner): GET reads only. POST /hari sets ktp_retention_days (1-3650, default 30); POST /hapus needs confirm=HAPUS, max 20 approved KTP files >= N days old per click; DB tx drops member_files + queues pending_file_cleanup, then post-commit portal.reconcile_pending. Verification rows kept. No cron/onload.
