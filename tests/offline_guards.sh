#!/bin/sh
# Root required, but all cases must fail BEFORE apt is invoked.
set -eu
installer=${1:?Path to install-offline.sh required}
installer=$(readlink -f "$installer")
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
mkdir "$temporary/packages"
cp /etc/os-release "$temporary/build-os-release"
printf 'wrong-architecture\n' > "$temporary/build-architecture"
printf 'not-a-package\n' > "$temporary/packages/test.deb"
cp "$installer" "$temporary/install-offline.sh"
checksum() { (cd "$temporary" && sha256sum packages/*.deb build-os-release build-architecture install-offline.sh > SHA256SUMS); }
reject() {
    if sh "$temporary/install-offline.sh" > "$temporary/result" 2>&1; then
        echo 'FAIL: unsafe input accepted' >&2; exit 1
    fi
    grep -q "$1" "$temporary/result"
}
checksum
reject 'Wrong target architecture'
dpkg --print-architecture > "$temporary/build-architecture"
sed -i 's/^ID=.*/ID=wrong-test-os/' "$temporary/build-os-release"
checksum
reject 'Wrong OS/release'
cp /etc/os-release "$temporary/build-os-release"
checksum
printf 'tampered\n' >> "$temporary/packages/test.deb"
reject 'FAILED'
echo 'PASS: wrong architecture, wrong OS and checksum failure rejected before installation'
