#!/bin/bash
# Build the .deb from this checkout and install it. Run: sudo bash install.sh
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
cd "$(dirname "$0")"
bash build-deb.sh
deb="$(ls -t build | grep '^cz-tailscale_.*_all\.deb$' | head -1)"
# apt's download user cannot read a home directory; install from a copy.
cp "build/$deb" /tmp/ && apt-get install -y "/tmp/$deb" && rm -f "/tmp/$deb"
tailscale debug prefs 2>/dev/null | grep OperatorUser || true
echo 'INSTALLED. Open "Peers" in the launcher. Uninstall: sudo apt remove cz-tailscale'
