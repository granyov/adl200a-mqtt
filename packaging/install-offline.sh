#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run: sudo sh install-offline.sh' >&2; exit 1; }
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"
sha256sum -c SHA256SUMS
architecture=$(dpkg --print-architecture)
[ "$architecture" = "$(cat build-architecture)" ] || { echo 'Wrong target architecture.' >&2; exit 1; }
# Read the known keys as data; do not source a bundle-provided shell script.
key() { sed -n "s/^$1=//p" "$2" | tr -d '\"'; }
for name in ID VERSION_ID VARIANT_ID; do
    [ "$(key "$name" /etc/os-release)" = "$(key "$name" build-os-release)" ] || {
        echo "Wrong OS/release ($name differs). Build a bundle on this target release." >&2
        exit 1
    }
done
if [ -f /etc/astra_version ] && [ -f build-astra-version ]; then
    cmp -s /etc/astra_version build-astra-version || { echo 'Astra version differs.' >&2; exit 1; }
fi
empty_lists=$(mktemp -d)
trap 'rm -rf "$empty_lists"' EXIT HUP INT TERM
mkdir "$empty_lists/partial"
mkdir "$empty_lists/sources.d"
touch "$empty_lists/sources.list"
export DEBIAN_FRONTEND=noninteractive
apt-get -y --no-download --no-install-recommends \
    -o "Dir::Etc::sourcelist=$empty_lists/sources.list" -o "Dir::Etc::sourceparts=$empty_lists/sources.d" \
    -o "Dir::State::lists=$empty_lists" -o "Dir::Cache::archives=$here/packages" \
    -o Dpkg::Options::=--force-confold \
    install "$here"/packages/*.deb
echo 'Offline installation complete. Set the physical port:'
echo '  sudo adl200a-configure /dev/ttyS5 38400'
