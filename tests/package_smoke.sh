#!/bin/sh
# Run INSIDE a disposable Debian root, after install-offline.sh.
set -eu
deb=${1:?Path to application deb required}
dpkg-query -W -f='${Status}\n' adl200a-mqtt | grep -qx 'install ok installed'
PYTHONPATH=/opt/adl200a/vendor python3 -c 'import serial, paho.mqtt.client; assert serial.VERSION == "3.5"'
getent passwd adl200a
[ "$(stat -c %U /var/lib/adl200a)" = adl200a ]
[ "$(cat /etc/adl200a/sensors.json)" = '{"channels": {}}' ]
grep -qx 'MaxRetentionSec=14day' /etc/systemd/journald.conf.d/zz-adl200a-retention.conf
grep -qx 'Storage=persistent' /etc/systemd/journald.conf.d/zz-adl200a-retention.conf
grep -qx 'User=adl200a' /lib/systemd/system/adl200a.service
# Preserve real user configuration and pending data through an upgrade.
sed -i 's|^ADL200A_PORT=.*|ADL200A_PORT=/dev/ttyS4|' /etc/default/adl200a
printf '{"channels":{"04":{"name":"preserve-me"}}}\n' > /etc/adl200a/sensors.json
printf '{"version":1,"cycle_id":"preserve-me","messages":[["test","123"]]}\n' > /var/lib/adl200a/pending.json
sha256sum /etc/default/adl200a /etc/adl200a/sensors.json /var/lib/adl200a/pending.json > /tmp/adl200a-before-upgrade.sha256
mkdir -p /etc/systemd/system
printf '[Service]\nExecStart=/usr/bin/python3 /opt/adl200a/adl200a_mqtt.py\n' > /etc/systemd/system/adl200a.service
dpkg -i "$deb"
sha256sum -c /tmp/adl200a-before-upgrade.sha256
[ ! -e /etc/systemd/system/adl200a.service ]
ls /var/backups/adl200a/legacy-service-* >/dev/null
[ "$(stat -c %U /var/lib/adl200a/pending.json)" = adl200a ]
dpkg --purge adl200a-mqtt
[ -f /var/lib/adl200a/pending.json ]
echo 'PASS: fresh offline install, vendored libraries, 14-day config, migration, upgrade preservation, pending data survives purge'
