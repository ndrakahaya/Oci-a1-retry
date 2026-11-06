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
`oci_retry.py`: coba `launch_instance` tiap 90 detik; kalau "Out of capacity"
terus coba lagi. Satu job jalan ~330 menit lalu memicu job berikutnya
(relay via `GH_PAT`); kalau instance berhasil dibuat, workflow menonaktifkan
dirinya sendiri.

## Secrets yang dibutuhkan (Settings → Secrets and variables → Actions)

| Secret | Isi |
| --- | --- |
| `OCI_USER` | OCID user OCI |
| `OCI_FINGERPRINT` | Fingerprint API key |
| `OCI_TENANCY` | OCID tenancy |
| `OCI_REGION` | `ap-singapore-1` |
| `OCI_PRIVATE_KEY` | Isi file `.pem` API key |
| `GH_PAT` | GitHub Personal Access Token (scope `workflow`) untuk relay + auto-disable |

Yang boleh dilihat publik di repo ini: tenancy OCID + SSH public key
(bukan rahasia). Private key API + PAT hanya ada di Secrets, tidak pernah
di-commit.
