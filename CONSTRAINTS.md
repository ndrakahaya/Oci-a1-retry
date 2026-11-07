# Constraints — Oci-a1-retry

Terakhir ditinjau: 2026-11-07 oleh RxSpeed (bersama user, desain Claude Sonnet).

Repo ini merebut Oracle Cloud Always Free VM.Standard.A1.Flex lewat GitHub
Actions. Bar di bawah ini adalah kontrak tertulis — bukan prosa yang boleh
diikuti boleh tidak. Setiap angka punya alasan dan perintah yang memeriksanya.
Jangan lunakkan file ini supaya sebuah perubahan lolos.

Baca file ini sebelum mengubah `oci_retry.py`, `resize_instance.py`,
`oidc_exchange.sh`, atau `.github/workflows/oci_retry.yml`.

## Floor (selalu ditegakkan, tanpa setup)

- Tidak ada secret di source: API key, private key `.pem`, UPST token, OIDC
  `client_secret`, `GH_PAT`. Secret hanya hidup di GitHub Secrets.
- Tidak ada stub yang belum diimplementasi dan tidak ada `except: pass` yang
  menelan kegagalan diam-diam tanpa komentar.
- Tidak ada test/check yang di-skip atau dihapus tanpa alasan di commit message.
- File ini tidak boleh dilunakkan supaya sebuah perubahan lolos.

## Ditegakkan dengan angka

| Dimensi | Aturan | Diperiksa oleh | Jalan di |
|---------|--------|----------------|----------|
| Siklus launch | Tepat 1 panggilan `launch_instance` per siklus; tidak pernah 2 beruntun | `python check_invariants.py` | CI, tiap push/PR |
| Alternasi shape | Shape bergantian 2/12 ↔ 1/6 tiap siklus (`SHAPE_LADDER`, `launch_no % len`) | `python check_invariants.py` | CI, tiap push/PR |
| Rotasi fault domain | Fault domain berotasi di 3 FD (`FAULT_DOMAINS`, `launch_no % len`) | `python check_invariants.py` | CI, tiap push/PR |
| Interval retry | `RETRY_INTERVAL` 90 detik antar siklus; `BACKOFF_429` 300 detik setelah 429 | `python check_invariants.py` | CI, tiap push/PR |
| Budget run | `timeout 2700` (45 menit) di workflow; keluar 124 = relay | `python check_invariants.py` | CI, tiap push/PR |
| Finish step | SATU langkah `Finish`, `case` pada `SCRIPT_EXIT`: 0 = issue + disable, 124 = relay, lain = job merah (`exit 1`), tanpa relay | `python check_invariants.py` | CI, tiap push/PR |
| Relay tidak mati diam-diam | Setiap panggilan GitHub API lewat helper `api()`: GH_PAT dulu, lalu token bawaan, 3 ronde; gagal semua = `return 1` (job merah, email terkirim) | `python check_invariants.py` | CI, tiap push/PR |
| Notifikasi sukses | Cabang 0 membuka GitHub issue yang me-mention pemilik repo, lalu menonaktifkan workflow | `python check_invariants.py` | CI, tiap push/PR |
| Duplicate guard | Guard cocokkan SHAPE saja (`VM.Standard.A1.Flex`), bukan `display_name` | `python check_invariants.py` | CI, tiap push/PR |
| Ambil IP publik | `get_public_ip` polling sampai 150 detik, tiap poll punya try/except sendiri; tidak pernah menggagalkan run | `python check_invariants.py` | CI, tiap push/PR |
| Penanganan 409 | `NotAuthorizedOrResourceAlreadyExists` → cek ulang instance sebelum mati | `python check_invariants.py` | CI, tiap push/PR |
| Anti double-create | Error jaringan yang hasilnya tak pasti → cek ulang instance sebelum retry | `python check_invariants.py` | CI, tiap push/PR |
| Batas instance | `MAX_A1_INSTANCES` = 1; tidak pernah launch A1 kedua | `python check_invariants.py` | CI, tiap push/PR |
| Setup retry | Panggilan setup (tenancy, jaringan, AD, image) lewat `with_retries`: `SETUP_TRIES` = 5; 4xx non-429 non-5xx = exit 2 (tanpa relay); gagal 5× = exit 1 (job merah) | `python check_invariants.py` | CI, tiap push/PR |
| Token kedaluwarsa | 401 setelah `MIN_RUN_BEFORE_RELAY_S` = 300 detik = token kedaluwarsa di tengah run → exit 124 (relay, token baru) | `python check_invariants.py` | CI, tiap push/PR |
| Jaringan idempoten | `create_vcn_and_subnet` memeriksa gateway, route, dan port di SETIAP run dan hanya membuat yang belum ada; aturan ingress lama dipertahankan (termasuk ICMP) | `python check_invariants.py` | CI, tiap push/PR |
| Kode keluar | 0 = sukses, 124 = timeout/token kedaluwarsa (relay), 2 = config/auth/kuota (tanpa relay), 1 = setup gagal 5× (tanpa relay) | `python check_invariants.py` | CI, tiap push/PR |
| Format log | Setiap baris log memakai `stamp()`: `UTC | WIB` | `python check_invariants.py` | CI, tiap push/PR |
| Simulasi | Perubahan pada `oci_retry.py` wajib lolos simulasi loop sebelum merge | `python test_retry_simulation.py` | CI, tiap push/PR |
| Secret di source | Tidak ada pola secret (PEM, token, `client_secret`, `GH_PAT`) di file | `python check_invariants.py` | CI, tiap push/PR |
| Secret di-mask | `oidc_exchange.sh` memakai `::add-mask::` untuk `CLIENT_SECRET`, `ID_TOKEN`, `UPST` | `python check_invariants.py` | CI, tiap push/PR |
| SDK di-pin | `pip install oci==2.190.0` (bukan `oci` tanpa versi) | `python check_invariants.py` | CI, tiap push/PR |
| curl exchange ber-timeout | `curl` di `oidc_exchange.sh` WAJIB pakai `--max-time` dan `--retry` | `python check_invariants.py` | CI, tiap push/PR |
| Watchdog | `watchdog.yml` jalan tiap 30 menit (cron `7,37`); restart hanya kalau rantai mati DAN 3 run terakhir TIDAK semuanya gagal (circuit breaker); tidak restart kalau workflow nonaktif atau ada run hidup | `python check_invariants.py` | CI, tiap push/PR |
| Resize | `resize_instance.py` + `resize.yml`: tanpa `CONFIRM_REBOOT=true` tidak ada yang diubah (exit 2); target di atas jatah Always Free (4 OCPU / 24 GB) ditolak (exit 2); retry kapasitas tiap 90 detik sampai `BUDGET_S` = 2580 (43 menit), habis = exit 124 | `python check_invariants.py` | CI, tiap push/PR |

Alasan tiap angka:
- 1 launch/siklus: 2 launch beruntun = 429 di panggilan ke-2 TIAP siklus
  (run #308: 7 attempt/45 mnt, bukan ~30; rung 1/6 tidak pernah teruji).
- Alternasi 2/12↔1/6: 1/6 muat di lebih banyak host daripada 2/12.
- Rotasi 3 FD: 2 shape × 3 FD = tiap pasangan kena 1× per 6 panggilan (~9 mnt).
- 90 detik: 30 attempt per run 2700 detik (Claude, run #311: gap 92,5 detik).
- 300 detik backoff 429: cukup lama supaya throttle Oracle reda.
- Relay hanya 124: exit lain = auth/config rusak; relay akan memutar loop
  gagal yang ketat dan membakar quota Actions.
- `api()` 3 ronde PAT→bawaan: PAT kedaluwarsa saja tidak boleh membunuh rantai
  (dulu `curl -s` menelan 401 dan tetap menulis "Next Job triggered").
- Issue + job merah: GitHub tidak mengirim email untuk run hijau; kegagalan
  cepat harus membuat job merah supaya email terkirim.
- Setup retry 5×: SATU error sementara (429/5xx) di fase setup dulu membunuh
  rantai tanpa suara (exit 1, langkah hijau, tidak ada job berikutnya).
- Token 300 detik: token yang lolos cek startup lalu 401 = kedaluwarsa di tengah
  run; relay memberi token baru. 401 sebelum 300 detik = config rusak (exit 2).
- Jaringan idempoten: run yang mati di tengah pembuatan VCN dulu meninggalkan
  VCN tanpa gateway/route/port — instance bisa jadi tapi tidak bisa dijangkau.
  Mengganti seluruh daftar ingress juga menghapus aturan ICMP bawaan.
- Guard shape-only: allowance Always Free = satu A1 total, apapun namanya.
- 150 detik poll IP: VNIC masih ATTACHING sesaat setelah launch.
- 409 + error tak pasti: instance bisa sudah terbuat di antara guard dan launch.
- SDK di-pin 2.190.0: versi yang sudah terbukti jalan (diagnose.yml mem-pin
  yang sama). Rilis SDK yang merusak akan mematikan semua run sekaligus.
- curl exchange pakai timeout+retry: tanpa itu, endpoint token yang hang
  memblokir run sampai job timeout 50 menit.
- Watchdog 30 menit + circuit breaker 3 gagal: rantai yang mati diam-diam adalah
  mode gagal paling mahal (kapasitas muncul tapi tidak ada yang mencoba); tapi
  masalah config yang di-restart terus hanya membakar quota Actions.
- Resize 43 menit + exit 124: resize juga bisa kena "Out of host capacity";
  habis budget = coba lagi nanti, bukan gagal. Tanpa konfirmasi, reboot tidak
  boleh terjadi diam-diam.

## Diukur, belum ditegakkan

| Metrik | Hari ini | Arah |
|--------|----------|------|
| Attempt per run 45 menit | ~30 (run #311: 7 attempt dalam 7 menit pertama, 0× 429) | harus tetap ~30 |
| 429 per run | 0 (run #311) | harus tetap 0 |

## Pengecualian

| ID | Aturan | Path | Alasan | Owner | Kedaluwarsa |
|----|--------|------|--------|-------|-------------|
| — | — | — | (belum ada) | — | — |

## Bukti simulasi

Sebelum merge perubahan pada `oci_retry.py`, jalankan `python
test_retry_simulation.py` dan pastikan lolos. Test ini men-stub SDK OCI dan
menjalankan loop retry melawan respons kalengan (Out of capacity, 429,
sukses, 409, error jaringan) — test ini yang membuktikan invarian di atas,
bukan sekadar membaca log. CI menjalankan test ini di tiap push/PR; merge
yang gagal simulasi ditolak.
