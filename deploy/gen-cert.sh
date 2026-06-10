#!/usr/bin/env bash
#
# Generate a long-lived self-signed TLS certificate for serving the HeaterMeter
# dashboard over HTTPS on the local network. HTTPS gives the app a secure
# context, which is what unlocks installing it as a PWA and real web push.
#
#   bash deploy/gen-cert.sh                # uses this host's name + IPs
#   HOST=bbq.local IP=192.168.3.164 bash deploy/gen-cert.sh
#
# Output: data/certs/hm.crt + data/certs/hm.key (key chmod 600).
# Then enable TLS via a systemd drop-in (no unit edit needed):
#   sudo systemctl edit heatermeterd
#     [Service]
#     Environment=HM_SSL_CERT=<repo>/data/certs/hm.crt
#     Environment=HM_SSL_KEY=<repo>/data/certs/hm.key
#   sudo systemctl restart heatermeterd
#
# Browsers will warn once about the self-signed cert; accept it for the device.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/data/certs"
HOST="${HOST:-$(hostname)}"
IP="${IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
DAYS="${DAYS:-3650}"

mkdir -p "$OUT"
SAN="DNS:$HOST,DNS:$HOST.local,DNS:localhost,IP:127.0.0.1"
[ -n "$IP" ] && SAN="$SAN,IP:$IP"

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout "$OUT/hm.key" -out "$OUT/hm.crt" \
  -subj "/CN=$HOST" -addext "subjectAltName=$SAN"
chmod 600 "$OUT/hm.key"

echo
echo "Wrote $OUT/hm.crt and $OUT/hm.key (valid $DAYS days, SAN: $SAN)"
echo "Enable with: HM_SSL_CERT=$OUT/hm.crt HM_SSL_KEY=$OUT/hm.key (env or flags)"
