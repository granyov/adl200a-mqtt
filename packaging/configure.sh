#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo.' >&2; exit 1; }
port=${1:?Usage: adl200a-configure /dev/ttyS5 [38400]}
baud=${2:-38400}
case "$port" in /dev/ttyS[0-9]*|/dev/ttyUSB[0-9]*|/dev/serial/by-id/*) ;; *) echo 'Invalid serial port.' >&2; exit 1 ;; esac
[ -c "$port" ] || { echo "Serial port not found: $port" >&2; exit 1; }
case "$port" in *[!a-zA-Z0-9/_:.+-]*) echo 'Unsupported character in serial port path.' >&2; exit 1 ;; esac
case "$baud" in 9600|19200|38400|57600|115200) ;; *) echo 'Unsupported baud rate.' >&2; exit 1 ;; esac
config=/etc/default/adl200a
cp -a "$config" "$config.bak"
sed -i "s|^ADL200A_PORT=.*|ADL200A_PORT=$port|; s|^ADL200A_BAUD=.*|ADL200A_BAUD=$baud|" "$config"
if grep -q '^ADL200A_MQTT_HOST=127.0.0.1$' "$config"; then
    systemctl enable --now mosquitto
fi
systemctl enable adl200a
systemctl restart adl200a
systemctl --no-pager --full status adl200a
