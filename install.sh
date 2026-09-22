#!/bin/sh
# install.sh — install the ADL-200A -> MQTT service. Run as root (or via sudo).
set -e
PREFIX=/opt/adl200a
SVCUSER="${SUDO_USER:-$(id -un)}"
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

echo "[*] dependencies"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || true
    apt-get install -y --no-install-recommends \
        python3-serial python3-paho-mqtt mosquitto mosquitto-clients setserial
    systemctl enable --now mosquitto || true
else
    echo "    (no apt; ensure python3-serial, paho-mqtt and an MQTT broker are present)"
fi

echo "[*] files -> $PREFIX"
install -d -m 755 "$PREFIX"
install -m 755 adl200a_mqtt.py ace.py sniffer.py prestart.sh "$PREFIX"/

echo "[*] config -> /etc/default/adl200a (kept if it already exists)"
[ -f /etc/default/adl200a ] || install -m 644 adl200a.default /etc/default/adl200a

echo "[*] service (User=$SVCUSER)"
sed "s/__USER__/$SVCUSER/" adl200a.service > /etc/systemd/system/adl200a.service
getent group dialout >/dev/null || groupadd dialout || true
id "$SVCUSER" >/dev/null 2>&1 && usermod -aG dialout "$SVCUSER" 2>/dev/null || true
systemctl daemon-reload

echo
echo "Installed. Next:"
echo "  1) set the serial port:   editor /etc/default/adl200a   (ADL200A_PORT=...)"
echo "  2) start the service:     systemctl enable --now adl200a"
echo "  3) watch the data:        mosquitto_sub -h 127.0.0.1 -t 'adl200a/#' -v"
echo "  4) manual command:        systemctl stop adl200a && python3 $PREFIX/ace.py MEA"
