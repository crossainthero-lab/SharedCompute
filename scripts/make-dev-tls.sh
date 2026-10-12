#!/bin/sh
# Create a private CA and a server certificate for a pilot marketplace.
#
#   scripts/make-dev-tls.sh OUT_DIR HOSTNAME [IP ...]
#   e.g. scripts/make-dev-tls.sh ~/sc-tls market.lan 192.168.1.20
#
# Outputs in OUT_DIR:
#   ca.crt      give this to provider machines (agent --ca-file ca.crt). Not secret.
#   ca.key      keep offline; anyone holding it can impersonate your marketplace.
#   server.crt  server.key  for `python -m sharedcompute serve --tls-cert ... --tls-key ...`
#
# Prefer a real certificate when you have a domain (Caddy / Let's Encrypt) or Tailscale (`tailscale cert`);
# see docs/REMOTE_CONNECTIVITY.md. Never commit these files.
set -eu
if [ $# -lt 2 ]; then
  echo "usage: $0 OUT_DIR HOSTNAME [IP ...]" >&2
  exit 2
fi
out=$1
host=$2
shift 2
san="DNS:$host"
for ip in "$@"; do san="$san,IP:$ip"; done

umask 077
mkdir -p "$out"
cd "$out"

cat > ca.cnf <<EOF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = SharedCompute pilot CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

cat > server.ext <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $san
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
EOF

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 -keyout ca.key -out ca.crt -config ca.cnf 2>/dev/null
openssl req -new -newkey rsa:2048 -nodes -sha256 -keyout server.key -out server.csr -subj "/CN=$host" 2>/dev/null
openssl x509 -req -sha256 -days 397 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -extfile server.ext 2>/dev/null
rm -f server.csr ca.srl ca.cnf server.ext
chmod 600 ca.key server.key
chmod 644 ca.crt server.crt
echo "Created $out/{ca.crt,ca.key,server.crt,server.key} for $san"
