#!/usr/bin/env python3
"""Build without network, pip, or root. Requires Linux dpkg-deb and Python 3.7+."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.1"
WHEELS = {
    "typing_extensions-4.7.1-py3-none-any.whl": "440d5dd3af93b060174bf433bccd69b0babc3b15b1a8dca43789fd7f61514b36",
    "pyserial-3.5-py2.py3-none-any.whl": "c4451db6ba391ca6ca299fb3ec7bae67a5c55dde170964c7a14ceefec02f2cf0",
    "paho_mqtt-2.1.0-py3-none-any.whl": "6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee",
}


def main():
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="adl200a-deb-") as temporary:
        stage = Path(temporary)
        def write(name, text, mode=0o644):
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)
        for name in ("adl200a_mqtt.py", "delivery.py", "calibration.py", "ace.py",
                     "sniffer.py", "channels.py", "live.sh", "prestart.sh"):
            write("opt/adl200a/" + name, (ROOT / name).read_text(), 0o755)
        vendor = stage / "opt/adl200a/vendor"
        vendor.mkdir()
        for filename, expected in WHEELS.items():
            wheel = ROOT / "packaging/wheels" / filename
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
                raise ValueError("dependency checksum mismatch: " + filename)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(vendor)
        write("opt/adl200a/run", '#!/bin/sh\nexport PYTHONPATH="/opt/adl200a/vendor${PYTHONPATH:+:$PYTHONPATH}"\nexec /usr/bin/python3 /opt/adl200a/adl200a_mqtt.py\n', 0o755)
        unit = (ROOT / "adl200a.service").read_text().replace("User=__USER__", "User=adl200a")
        unit = unit.replace("ExecStart=/usr/bin/env python3 /opt/adl200a/adl200a_mqtt.py",
                            "ExecStart=/opt/adl200a/run")
        unit = unit.replace("EnvironmentFile=", "Environment=PYTHONUNBUFFERED=1\nEnvironment=PYTHONPATH=/opt/adl200a/vendor\nEnvironmentFile=")
        write("lib/systemd/system/adl200a.service", unit)
        write("etc/default/adl200a", (ROOT / "adl200a.default").read_text().replace(
            "ADL200A_PORT=/dev/ttyUSB0", "ADL200A_PORT=/dev/ttyS5"))
        write("etc/adl200a/sensors.json", '{"channels": {}}\n')
        write("etc/systemd/journald.conf.d/zz-adl200a-retention.conf",
              (ROOT / "packaging/journald.conf").read_text())
        for name in ("postinst", "prerm", "postrm"):
            write("DEBIAN/" + name, (ROOT / "packaging" / name).read_text(), 0o755)
        write("usr/sbin/adl200a-configure", (ROOT / "packaging/configure.sh").read_text(), 0o755)
        write("usr/share/doc/adl200a-mqtt/README.offline.md", (ROOT / "packaging/README.md").read_text())
        write("usr/share/doc/adl200a-mqtt/copyright", (ROOT / "LICENSE").read_text())
        installed_size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024
        write("DEBIAN/control", """Package: adl200a-mqtt
Version: %s
Section: electronics
Priority: optional
Architecture: all
Maintainer: Kirill E. Granyov <granyov@users.noreply.github.com>
Depends: python3 (>= 3.7), systemd, adduser
Recommends: mosquitto, mosquitto-clients, setserial
Installed-Size: %d
Homepage: https://github.com/granyov/adl200a-mqtt
Description: ACE ADL-200A RS-232 to MQTT with confirmed delivery
 Bundled pyserial 3.5, paho-mqtt 2.1.0 and typing_extensions 4.7.1.
 One durable pending batch,
 QoS 1 acknowledgements, automatic reconnect and 14-day persistent logs.
""" % (VERSION, installed_size))
        write("DEBIAN/conffiles", "\n".join(("/etc/default/adl200a", "/etc/adl200a/sensors.json",
              "/etc/systemd/journald.conf.d/zz-adl200a-retention.conf")) + "\n")
        output = destination / ("adl200a-mqtt_%s_all.deb" % VERSION)
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(stage), str(output)], check=True)
        print(output)


if __name__ == "__main__":
    main()
