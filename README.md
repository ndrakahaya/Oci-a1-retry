# oci-a1-retry

Retry otomatis 24/7 untuk merebut Oracle Cloud Always Free `VM.Standard.A1.Flex`
(Ubuntu 24.04, fallback 22.04, boot volume 100 GB, nama instance `agent-box`) di tenancy
indrawinatake6, region ap-singapore-1 (1 AD, 3 fault domain), lewat GitHub Actions.

Dasar: [WendyChen330/oci-arm-retry](https://github.com/WendyChen330/oci-arm-retry),
disesuaikan: login OIDC (tidak ada API key di repo), tangga ukuran, rotasi fault domain,
VCN `retry-vcn` + subnet `retry-subnet` dibuat atau dilengkapi otomatis.

## Cara kerja

1. `.github/workflows/oci_retry.yml` (dijalankan manual lewat *Run workflow*) menukar JWT OIDC
   GitHub dengan session token Oracle (`oidc_exchange.sh`), lalu menjalankan `oci_retry.py`
   maksimal 45 menit (token Oracle berlaku sekitar 60 menit).
2. `oci_retry.py` mencoba **satu** `launch_instance` tiap 90 detik. Ukuran bergantian
   2 OCPU / 12 GB dan 1 OCPU / 6 GB, fault domain: 4 dari 7 panggilan tidak mengunci FD (`ANY-FD`, Oracle memilih sendiri sambil mengoptimalkan kapasitas), sisanya mengunci FD-1, FD-2, FD-3 bergantian. "Out of capacity" berarti
   coba lagi. Satu panggilan per siklus karena dua panggilan beruntun selalu kena 429 (run #308).
3. Sebelum tiap siklus script mengecek apakah sudah ada instance A1 (nama apa pun). Kalau ada, berhenti.
4. Setelah 45 menit script keluar dengan kode 124 dan langkah *Finish* memicu job berikutnya
   (relay). Jeda antar-job sekitar 1–2 menit untuk install dan tukar token.
5. Instance berhasil dibuat → *Finish* membuka issue yang me-mention pemilik repo (GitHub tidak
   mengirim email untuk run yang hijau), lalu menonaktifkan workflow. Kalau yang jadi 1/6,
   naikkan ke 2/12 dengan workflow *OCI A1 Resize* (lihat di bawah).

Waktu di log ditulis `UTC | WIB`.

## Kode keluar `oci_retry.py` dan reaksi workflow

| Kode | Arti | Workflow |
| --- | --- | --- |
| 0 | instance sudah ada atau berhasil dibuat | issue notifikasi, lalu workflow dinonaktifkan |
| 124 | budget 45 menit habis, atau token sesi kedaluwarsa di tengah jalan | relay ke job berikutnya |
| 2 | masalah config / auth / izin / kuota (mis. 404, LimitExceeded) | job merah (email gagal), tidak relay |
| 1 | setup (tenancy, jaringan, AD, image) gagal terus setelah 5 percobaan | job merah, tidak relay |

Kegagalan di langkah lain (tukar token, verifikasi) juga membuat job merah. Error sementara
di fase setup (429, 5xx, jaringan) dicoba ulang dulu sebelum menyerah.

## Setelan yang dibutuhkan (Settings → Secrets and variables → Actions)

| Nama | Jenis | Isi |
| --- | --- | --- |
| `OIDC_CLIENT_IDENTIFIER` | secret | `client_id:client_secret` Confidential App di Identity Domain |
| `DOMAIN_BASE_URL` | variable | base URL Identity Domain |
| `OCI_TENANCY` | variable | OCID tenancy (script tetap memakai nilai dari Oracle sendiri) |
| `GH_PAT` | secret | PAT (scope `workflow`) untuk relay dan auto-disable. Opsional: kalau kosong atau kedaluwarsa, dipakai token bawaan workflow |

OCID user layanan tertulis langsung di workflow (bukan rahasia). Secret `OCI_USER`,
`OCI_FINGERPRINT`, `OCI_TENANCY`, `OCI_REGION`, `OCI_PRIVATE_KEY` hanya dipakai `diagnose.yml`
(alur API key lama).

Pastikan Trust rule di Oracle mengunci klaim `sub` ke repo dan branch ini. Klaim yang
sebenarnya dicetak di log langkah tukar token.

## Naikkan ke 2/12 (kalau yang menang 1/6)

Jalankan workflow **OCI A1 Resize** (`resize.yml`, *Run workflow*) dan centang `confirm_reboot`.
`resize_instance.py` mengubah instance A1 ke 2 OCPU / 12 GB lewat `update_instance`.

- Mengubah shape **me-reboot** instance yang sedang jalan. IP publik, IP privat, dan disk tetap.
  Hentikan aplikasi penting dulu. Tanpa centang, tidak ada yang diubah: job merah dengan pesan
  "Not confirmed" dan ukuran sekarang tercetak di log.
- Resize juga bisa kena "Out of host capacity". Script mencoba lagi tiap 90 detik sampai 43 menit,
  lalu keluar dengan kode 124. Jalankan lagi nanti.
- Target di atas jatah Always Free (2 OCPU / 12 GB sejak 15 Juni 2026) ditolak.

## Watchdog (opsional)

`.github/workflows/watchdog.yml` jalan tiap 30 menit. Kalau `oci_retry.yml` aktif tapi tidak ada
run yang berjalan atau mengantre (rantai putus), ia menjalankannya lagi. Ia diam kalau workflow
sudah nonaktif, kalau rantai masih hidup, atau kalau 3 run terakhir semuanya gagal (butuh
campur tangan manusia). Untuk menghentikan semuanya dengan sengaja, nonaktifkan `oci_retry.yml`.

## Catatan

- Repo ini publik, jadi log Actions juga publik. IP publik dan perintah SSH yang dicetak setelah
  sukses ikut terlihat. Tenancy OCID dan SSH public key memang bukan rahasia. API key privat
  tidak ada di repo.
- Menjalankan ulang setelah job merah: perbaiki penyebabnya, lalu *Run workflow* lagi.
