#!/bin/sh
# Run on an online machine with exactly the target OS/release/architecture.
# Download only: never install/upgrade packages on the build host.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo.' >&2; exit 1; }
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
deb=${1:?Usage: build-offline.sh /path/adl200a-mqtt_1.0.1_all.deb /path/new-output-directory}
output=${2:?Specify a new output directory}
[ -f "$deb" ] || { echo 'Application deb not found.' >&2; exit 1; }
[ ! -e "$output" ] || { echo 'Output directory must not exist.' >&2; exit 1; }
mkdir -p "$output/packages/partial"
output=$(CDPATH= cd -- "$output" && pwd)
cp "$deb" "$output/packages/"
cp /etc/os-release "$output/build-os-release"
if [ -f /etc/astra_version ]; then cp /etc/astra_version "$output/build-astra-version"; fi
dpkg --print-architecture > "$output/build-architecture"
cp "$here/install-offline.sh" "$output/install-offline.sh"
cp "$here/README.md" "$output/README.md"
chmod 755 "$output/install-offline.sh"
touch "$output/empty-status"
apt-get update
# Empty status makes apt resolve the complete dependency closure rather than
# silently omit dependencies already installed on this build machine.
apt-get -y --download-only --no-install-recommends \
    -o "Dir::State::status=$output/empty-status" \
    -o "Dir::Cache::archives=$output/packages" \
    install "$output/packages/$(basename "$deb")" mosquitto mosquitto-clients setserial
rm "$output/empty-status"
rm -rf "$output/packages/partial"
rm -f "$output/packages/lock"
for package in "$output"/packages/*.deb; do
    dpkg-deb -f "$package" Package Version Architecture
done > "$output/package-versions.txt"
(cd "$output" && sha256sum packages/*.deb build-os-release build-architecture package-versions.txt install-offline.sh README.md > SHA256SUMS)
if [ -f "$output/build-astra-version" ]; then
    (cd "$output" && sha256sum build-astra-version >> SHA256SUMS)
fi
echo "Complete bundle: $output"
echo 'This bundle is only for the recorded OS release and architecture.'
