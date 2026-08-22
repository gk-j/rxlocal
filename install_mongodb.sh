#!/usr/bin/env bash
# Install MongoDB Community Edition 8.0 on Ubuntu 24.04 (noble), aarch64 or x86_64.
# DGX OS on the GB10 is Ubuntu 24.04, so this is the GB10 path.
#
#   ./scripts/install_mongodb.sh
#
# Binds to 127.0.0.1 only: the database is not reachable from the network.
set -euo pipefail

CODENAME=$(. /etc/os-release && echo "$UBUNTU_CODENAME")
ARCH=$(dpkg --print-architecture)
VERSION=8.0

echo "Ubuntu $CODENAME on $ARCH, installing MongoDB Community $VERSION"

if [ "$CODENAME" != "noble" ]; then
  echo "warning: this script targets noble (24.04); you are on $CODENAME" >&2
fi

# MongoDB 7.0 has no noble build. 8.0 does, for both arm64 and amd64.
sudo apt-get update
sudo apt-get install -y curl gnupg

curl -fsSL "https://www.mongodb.org/static/pgp/server-${VERSION}.asc" \
  | sudo gpg --dearmor -o "/usr/share/keyrings/mongodb-server-${VERSION}.gpg"

echo "deb [ arch=${ARCH} signed-by=/usr/share/keyrings/mongodb-server-${VERSION}.gpg ] \
https://repo.mongodb.org/apt/ubuntu ${CODENAME}/mongodb-org/${VERSION} multiverse" \
  | sudo tee "/etc/apt/sources.list.d/mongodb-org-${VERSION}.list" >/dev/null

sudo apt-get update
sudo apt-get install -y mongodb-org

# Default bindIp is already 127.0.0.1, but make it explicit rather than assumed.
if ! grep -qE '^\s*bindIp:\s*127\.0\.0\.1\s*$' /etc/mongod.conf; then
  echo "setting bindIp to 127.0.0.1 in /etc/mongod.conf"
  sudo sed -i 's/^\(\s*\)bindIp:.*/\1bindIp: 127.0.0.1/' /etc/mongod.conf
fi

sudo systemctl enable --now mongod
sleep 2

echo
echo "--- status ---"
systemctl is-active mongod
mongosh --quiet --eval 'print("mongod " + db.version())'
echo
echo "--- listening on ---"
ss -tlnp 2>/dev/null | grep 27017 || true
echo
echo "Done. Expect 127.0.0.1:27017 above - not 0.0.0.0."
