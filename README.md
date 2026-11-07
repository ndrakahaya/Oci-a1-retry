# oci-a1-retry

Retry otomatis 24/7 untuk merebut Oracle Cloud Always Free VM.Standard.A1.Flex
(2 OCPU / 12 GB RAM / 100 GB disk, Ubuntu 24.04, nama instance `agent-box`)
di tenancy indrawinatake6, region ap-singapore-1 (AD-1), lewat GitHub Actions.

Dasar: [WendyChen330/oci-arm-retry](https://github.com/WendyChen330/oci-arm-retry)
(MIT-style retry script), disesuaikan: instance `agent-box`, Ubuntu 24.04
(fallback 22.04), SSH key `indra-oracle-a1`, VCN `retry-vcn` + subnet
`retry-subnet` dibuat otomatis oleh script bila belum ada.

## Cara kerja

Workflow `.github/workflows/oci_retry.yml` (manual trigger) menjalankan
`oci_retry.py`: coba `launch_instance` tiap 90 detik (bergantian 2 OCPU/12 GB
dan 1 OCPU/6 GB, rotasi 3 fault domain); kalau "Out of capacity" terus coba
lagi. Satu job jalan ~45 menit lalu memicu job berikutnya (relay via
`GH_PAT`); kalau instance berhasil dibuat, workflow menonaktifkan dirinya
sendiri. Auth memakai OIDC (GitHub JWT → OCI session token, lihat
`oidc_exchange.sh`), bukan API key.

Pengaman rantai relay: `.github/workflows/watchdog.yml` (tiap jam) memeriksa
apakah rantai masih hidup; kalau mati diam-diam >2 jam, watchdog memicu run
baru. Notifikasi gagal fatal dikirim via ntfy
(`oci-a1-retry-ndrakahaya-2c085768`).

Aturan main ada di `CONSTRAINTS.md` dan ditegakkan otomatis oleh
`.github/workflows/quality-gate.yml` (`check_invariants.py` +
`test_retry_simulation.py`) di tiap push/PR.

## Secrets & variables yang dibutuhkan (Settings → Secrets and variables → Actions)

| Nama | Jenis | Isi |
| --- | --- | --- |
| `OIDC_CLIENT_IDENTIFIER` | Secret | `client_id:client_secret` aplikasi OIDC runtime (Identity Domain) |
| `DOMAIN_BASE_URL` | Variable | Base URL Identity Domain Oracle |
| `OCI_TENANCY` | Variable | OCID tenancy |
| `GH_PAT` | Secret | GitHub Personal Access Token (scope `workflow`) untuk relay + auto-disable + watchdog |

Yang boleh dilihat publik di repo ini: tenancy OCID + SSH public key
(bukan rahasia). Private key API + PAT + client secret hanya ada di Secrets,
tidak pernah di-commit.

Catatan: workflow lama `.github/workflows/diagnose.yml` memakai jalur API-key
(`OCI_USER` / `OCI_FINGERPRINT` / `OCI_PRIVATE_KEY`) — sudah tidak dipakai
jalur aktif, hanya untuk diagnosis manual.
