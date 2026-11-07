#!/bin/bash
# Manual OIDC token exchange: GitHub Actions JWT -> OCI UPST (session token).
#
# Replaces the gtrevorrow/oci-token-exchange-action black box with a transparent,
# fully debuggable exchange. Protocol reverse-engineered from that action's source
# (src/main.ts, src/platforms/github.ts) + Oracle's token-exchange docs:
#   - RSA 2048 keypair (ephemeral, per run)
#   - public_key = base64(DER SPKI); fingerprint = MD5(DER SPKI) colon-separated
#   - POST ${DOMAIN}/oauth2/v1/token, grant_type token-exchange,
#     requested_token_type urn:oci:token-type:oci-upst, subject_token_type "jwt",
#     Basic auth client_id:client_secret
#   - response JSON field "token" = UPST
#   - OCI config: user = a DUMMY OCID (valid format, not a real user) + NO auth=
#     line (SDK detects security_token_file) + security_token_file + key_file.
#     The UPST identifies the real user; the config user value is ignored by the
#     API. A real user OCID in user= makes the API reject the UPST with 401.
#     NOTE (2026-11-06 run #287): "user=not used" (as in the gtrevorrow action)
#     FAILS the current OCI SDK's config validation — its regex requires an
#     OCID-shaped value, so "not used" raises InvalidConfig {'user': 'malformed'}
#     before any API call. The dummy OCID passes validation and is ignored the
#     same way "not used" is.
#
# Env vars (set by the workflow from secrets/vars):
#   OIDC_CLIENT_IDENTIFIER = client_id:client_secret (Confidential App runtime)
#   DOMAIN_BASE_URL        = Identity Domain base URL
#   OCI_TENANCY            = tenancy OCID
#   OCI_REGION             = region (default ap-singapore-1)
#   OCI_USER               = service user OCID (kept for the Verify step; NOT written
#                            into the config user field — see above)
# Outputs: ~/.oci/config + ~/.oci/sessions/DEFAULT/{private_key.pem,public_key.pem,token}
# Debug: prints the decoded JWT payload claims (NOT the token itself) so the
# Identity Propagation Trust rule can be fixed precisely if the exchange 401s.
set -euo pipefail

DOMAIN="${DOMAIN_BASE_URL:?DOMAIN_BASE_URL not set}"
TENANCY="${OCI_TENANCY:?OCI_TENANCY not set}"
REGION="${OCI_REGION:-ap-singapore-1}"
SVC_USER="${OCI_USER:?OCI_USER not set}"
CLIENT_ID="${OIDC_CLIENT_IDENTIFIER%%:*}"
CLIENT_SECRET="${OIDC_CLIENT_IDENTIFIER#*:}"
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  echo "ERROR: OIDC_CLIENT_IDENTIFIER must be client_id:client_secret"
  exit 1
fi
# The secret is registered as "id:secret"; the halves are separate strings to the log masker.
echo "::add-mask::$CLIENT_SECRET"

OCI_DIR="$HOME/.oci"
SESS_DIR="$OCI_DIR/sessions/DEFAULT"
mkdir -p "$SESS_DIR"

# 1. Ephemeral RSA keypair (2048-bit)
openssl genrsa -out "$SESS_DIR/private_key.pem" 2048 2>/dev/null
openssl rsa -pubout -in "$SESS_DIR/private_key.pem" -out "$SESS_DIR/public_key.pem" 2>/dev/null
chmod 600 "$SESS_DIR/private_key.pem"

# 2. public_key = base64(DER SPKI); fingerprint = MD5(DER SPKI), colon-separated
PUBKEY_B64=$(openssl pkey -pubin -in "$SESS_DIR/public_key.pem" -outform DER 2>/dev/null | base64 -w0)
FINGERPRINT=$(openssl pkey -pubin -in "$SESS_DIR/public_key.pem" -outform DER 2>/dev/null | md5sum | awk '{print $1}' | sed 's/\(..\)/\1:/g; s/:$//')
echo "key fingerprint: $FINGERPRINT"

# 3. GitHub OIDC JWT (audience https://cloud.oracle.com)
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  echo "ERROR: not running in GitHub Actions (no ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN)"
  exit 1
fi
ID_TOKEN=$(curl -s --retry 3 --retry-delay 5 --retry-connrefused --max-time 30 \
  -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=https%3A%2F%2Fcloud.oracle.com" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")
if [ -z "$ID_TOKEN" ]; then
  echo "ERROR: failed to obtain GitHub OIDC token"
  exit 1
fi

echo "::add-mask::$ID_TOKEN"

# 4. DEBUG: decode JWT payload claims (shows the actual `sub` so the Trust rule can be fixed precisely)
PAYLOAD_B64=$(echo "$ID_TOKEN" | cut -d. -f2)
PAYLOAD_PADDED=$(echo "$PAYLOAD_B64" | tr '_-' '/+' | awk '{ l=length($0)%4; if(l==2) print $0"=="; else if(l==3) print $0"="; else print $0 }')
echo "--- GitHub OIDC token claims (payload) ---"
echo "$PAYLOAD_PADDED" | base64 -d 2>/dev/null | python3 -m json.tool
echo "--- end claims ---"

# 5. Exchange JWT for OCI UPST at the Identity Domain token endpoint
EXCHANGE_URL="${DOMAIN}/oauth2/v1/token"
RESP=$(curl -s --retry 3 --retry-delay 5 --retry-connrefused --max-time 30 -X POST "$EXCHANGE_URL" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "$CLIENT_ID:$CLIENT_SECRET" \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  --data-urlencode "requested_token_type=urn:oci:token-type:oci-upst" \
  --data-urlencode "public_key=$PUBKEY_B64" \
  --data-urlencode "subject_token=$ID_TOKEN" \
  --data-urlencode "subject_token_type=jwt")
echo "--- exchange response ---"
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'token' in d:
    print('UPST received, length:', len(d['token']))
else:
    print('EXCHANGE FAILED:', json.dumps(d, indent=2))
    sys.exit(1)
"
UPST=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
echo "::add-mask::$UPST"

# 6. Write session token + OCI config (security_token auth)
printf '%s' "$UPST" > "$SESS_DIR/token"
chmod 600 "$SESS_DIR/token"
cat > "$OCI_DIR/config" <<EOF
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
fingerprint=$FINGERPRINT
tenancy=$TENANCY
region=$REGION
key_file=$SESS_DIR/private_key.pem
security_token_file=$SESS_DIR/token
EOF
chmod 600 "$OCI_DIR/config"
echo "OCI config written: $OCI_DIR/config"
echo "EXCHANGE OK"
