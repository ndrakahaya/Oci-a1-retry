# Constraints — Oci-a1-retry

Terakhir ditinjau: 2026-11-07 oleh RxSpeed (bersama user).

Repo ini merebut Oracle Cloud Always Free VM.Standard.A1.Flex lewat GitHub
Actions. Bar di bawah ini adalah kontrak tertulis — bukan prosa yang boleh
diikuti boleh tidak. Setiap angka punya alasan dan perintah yang memeriksanya.
Jangan lunakkan file ini supaya sebuah perubahan lolos.

Baca file ini sebelum mengubah `oci_retry.py`, `oidc_exchange.sh`, atau
`.github/workflows/oci_retry.yml`.

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
| Relay | Relay job berikutnya HANYA bila `SCRIPT_EXIT == '124'` (timeout 45 menit). Exit lain = fast failure, jangan relay | `python check_invariants.py` | CI, tiap push/PR |
| Duplicate guard | Guard cocokkan SHAPE saja (`VM.Standard.A1.Flex`), bukan `display_name` | `python check_invariants.py` | CI, tiap push/PR |
| Ambil IP publik | `get_public_ip` polling sampai 150 detik, tiap poll punya try/except sendiri; tidak pernah menggagalkan run | `python check_invariants.py` | CI, tiap push/PR |
| Penanganan 409 | `NotAuthorizedOrResourceAlreadyExists` → cek ulang instance sebelum mati | `python check_invariants.py` | CI, tiap push/PR |
| Anti double-create | Error jaringan yang hasilnya tak pasti → cek ulang instance sebelum retry | `python check_invariants.py` | CI, tiap push/PR |
| Batas instance | `MAX_A1_INSTANCES` = 1; tidak pernah launch A1 kedua | `python check_invariants.py` | CI, tiap push/PR |
| Simulasi | Perubahan pada `oci_retry.py` wajib lolos simulasi loop sebelum merge | `python test_retry_simulation.py` | CI, tiap push/PR |
| Secret di source | Tidak ada pola secret (PEM, token, `client_secret`, `GH_PAT`) di file | `python check_invariants.py` | CI, tiap push/PR |

Alasan tiap angka:
- 1 launch/siklus: 2 launch beruntun = 429 di panggilan ke-2 TIAP siklus
  (run #308: 7 attempt/45 mnt, bukan ~30; rung 1/6 tidak pernah teruji).
- Alternasi 2/12↔1/6: 1/6 muat di lebih banyak host daripada 2/12.
- Rotasi 3 FD: 2 shape × 3 FD = tiap pasangan kena 1× per 6 panggilan (~9 mnt).
- 90 detik: 30 attempt per run 2700 detik (Claude, run #311: gap 92,5 detik).
- 300 detik backoff 429: cukup lama supaya throttle Oracle reda.
- Relay hanya 124: exit lain = auth/config rusak; relay akan memutar loop
  gagal yang ketat dan membakar quota Actions.
- Guard shape-only: allowance Always Free = satu A1 total, apapun namanya.
- 150 detik poll IP: VNIC masih ATTACHING sesaat setelah launch.
- 409 + error tak pasti: instance bisa sudah terbuat di antara guard dan launch.

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
