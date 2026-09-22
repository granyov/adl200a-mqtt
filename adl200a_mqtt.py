#!/usr/bin/env python3
"""ADL-200A (ACE Instrument) data-logger -> MQTT daemon.

Portable across hosts: all settings come from environment variables, so the same
code runs on a Moxa UPort 1150 (/dev/ttyUSB0) or a native RS-232 port (/dev/ttyS0).

Protocol (ACE ASCII over RS-232, reverse-engineered from ADL2Pro.exe, confirmed
on hardware):
  Request : >>?1,<ID>,<CMD>;\\r          (CR only, no checksum)
  Response: <<!01,<ID>,<CMD>:<data>;\\r
  MEA (measure all, ~26 s) -> per channel & type:
     <<!01,<ID>,MES:<CH>,<TYPE>,<yy/mm/dd>,<hh:mm:ss>,<VALUE>;\\r
     TYPE 01 = primary reading (VW digits/freq), TYPE 06 = secondary (temperature)

Env vars (defaults in parentheses):
  ADL200A_PORT (/dev/ttyUSB0)  ADL200A_BAUD (38400)  ADL200A_ID (1)
  ADL200A_INTERVAL (60)        ADL200A_MQTT_HOST (127.0.0.1)
  ADL200A_MQTT_PORT (1883)     ADL200A_TOPIC (adl200a)
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import serial
import paho.mqtt.client as mqtt

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))
LOGGER_ID = int(os.environ.get("ADL200A_ID", "1"))
POLL_INTERVAL = int(os.environ.get("ADL200A_INTERVAL", "60"))
MQTT_HOST = os.environ.get("ADL200A_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("ADL200A_MQTT_PORT", "1883"))
TOPIC = os.environ.get("ADL200A_TOPIC", "adl200a")

MEA_MAX_SECONDS = 45        # hard cap on one MEA read
MEA_IDLE_DONE = 3.0         # s of silence after data => cycle complete
READ_TIMEOUT = 0.5

MES_RE = re.compile(rb"<<!(\d+),(\d+),MES:(\d+),(\d+),([0-9/]+),([0-9:]+),(\d+);")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("adl200a")


def make_mqtt() -> mqtt.Client:
    try:  # paho-mqtt >= 2.0
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="adl200a")

        def on_connect(cl, u, flags, reason_code, props):
            log.info("MQTT connected: %s", reason_code)

        def on_disconnect(cl, u, flags, reason_code, props):
            log.warning("MQTT disconnected: %s", reason_code)
    except AttributeError:  # paho-mqtt < 2.0
        c = mqtt.Client(client_id="adl200a")

        def on_connect(cl, u, flags, rc):
            log.info("MQTT connected rc=%s", rc)

        def on_disconnect(cl, u, rc):
            log.warning("MQTT disconnected rc=%s", rc)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect_async(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    return c


def open_serial() -> serial.Serial:
    while True:
        try:
            s = serial.Serial(PORT, BAUD, bytesize=serial.EIGHTBITS,
                              parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                              timeout=READ_TIMEOUT)
            log.info("Serial open %s @ %d 8N1", PORT, BAUD)
            return s
        except serial.SerialException as e:
            log.error("Serial open failed: %s (retry 5s)", e)
            time.sleep(5)


def measure(ser: serial.Serial) -> bytes:
    """Send MEA and collect the full multi-frame response."""
    cmd = (">>?1,%d,MEA;\r" % LOGGER_ID).encode()
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()
    buf = bytearray()
    t0 = time.time()
    last = t0
    while time.time() - t0 < MEA_MAX_SECONDS:
        d = ser.read(1024)
        if d:
            buf += d
            last = time.time()
        elif buf and time.time() - last > MEA_IDLE_DONE:
            break
    return bytes(buf)


def parse(buf: bytes) -> dict:
    chans = {}
    for m in MES_RE.finditer(buf):
        _lid, _addr, ch, typ, date, tm, val = m.groups()
        ch = int(ch)
        c = chans.setdefault(ch, {"channel": ch, "values": {},
                                  "dev_date": date.decode(), "dev_time": tm.decode()})
        c["values"]["t%02d" % int(typ)] = int(val)
    return chans


def main() -> None:
    log.info("ADL-200A -> MQTT | port=%s baud=%d id=%d interval=%ds mqtt=%s:%d topic=%s",
             PORT, BAUD, LOGGER_ID, POLL_INTERVAL, MQTT_HOST, MQTT_PORT, TOPIC)
    client = make_mqtt()
    ser = open_serial()
    while True:
        start = time.monotonic()
        try:
            raw = measure(ser)
            chans = parse(raw)
            if chans:
                ts = datetime.now(timezone.utc).isoformat()
                for ch in sorted(chans):
                    payload = dict(chans[ch])
                    payload["timestamp"] = ts
                    client.publish("%s/ch/%02d" % (TOPIC, ch), json.dumps(payload), qos=0)
                client.publish("%s/measurement" % TOPIC, json.dumps(
                    {"timestamp": ts, "channels": [chans[c] for c in sorted(chans)]}), qos=0)
                log.info("Published %d channels (%d raw bytes)", len(chans), len(raw))
            else:
                log.warning("No MES frames parsed (%d bytes) - logger connected on %s?",
                            len(raw), PORT)
        except serial.SerialException as e:
            log.error("Serial error: %s (reconnecting)", e)
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial()
            continue
        except Exception as e:  # keep the daemon alive
            log.exception("Unexpected: %s", e)
        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
