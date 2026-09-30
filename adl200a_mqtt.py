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
  ADL200A_SENSORS (/etc/adl200a/sensors.json)

With a sensors file present, each channel payload also carries an "eng" object
(pressure, temperature, frequency) and the scalars are mirrored to
<topic>/ch/<NN>/{pressure,temperature,frequency,digits} for plain consumers.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibration

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))
LOGGER_ID = int(os.environ.get("ADL200A_ID", "1"))
POLL_INTERVAL = int(os.environ.get("ADL200A_INTERVAL", "60"))
MQTT_HOST = os.environ.get("ADL200A_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("ADL200A_MQTT_PORT", "1883"))
TOPIC = os.environ.get("ADL200A_TOPIC", "adl200a")
SENSORS_FILE = os.environ.get("ADL200A_SENSORS", "/etc/adl200a/sensors.json")

MEA_MAX_SECONDS = 45        # hard cap on one MEA read
MEA_IDLE_DONE = 6.0         # s of silence after data => give up on the rest
READ_TIMEOUT = 0.5

MES_RE = re.compile(rb"<<!(\d+),(\d+),MES:(\d+),(\d+),([0-9/]+),([0-9:]+),(\d+);")
GCC_RE = re.compile(rb"<<!\d+,\d+,GCC:(\d+),(\d+),")

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


def enabled_channels(ser: serial.Serial) -> set:
    """Ask GCC which channels the logger actually sweeps.

    A MEA answer arrives channel by channel with seconds of silence in between,
    so "no data for a while" is not a reliable end-of-sweep marker: it truncates
    the sweep and the leftover frames surface in the next cycle. Knowing how many
    channels to expect lets measure() stop exactly when the sweep is complete.
    """
    # Сначала дать прибору договорить: он долго досылает хвост развёртки, и
    # ответ на GCC приедет вперемешку с кадрами MES.
    t0 = last = time.time()
    while time.time() - t0 < 45:
        if ser.read(1024):
            last = time.time()
        elif time.time() - last > 3.0:
            break

    ser.reset_input_buffer()
    ser.write((">>?1,%d,GCC;\r" % LOGGER_ID).encode())
    ser.flush()
    buf = bytearray()
    t0 = time.time()
    last = t0
    while time.time() - t0 < 30:
        d = ser.read(1024)
        if d:
            buf += d
            last = time.time()
            if len(GCC_RE.findall(bytes(buf))) >= 16:
                break
        elif buf and time.time() - last > 2.0:
            break
    seen = GCC_RE.findall(bytes(buf))
    if len(seen) < 16:
        # Неполный ответ — это "прибор не договорил", а не "каналов меньше".
        # Лучше ничего не знать, чем знать неправду: measure() тогда уйдёт на
        # запасной порог тишины.
        log.warning("GCC пришёл неполным (%d из 16) — прибор занят", len(seen))
        return set()
    return set(int(ch) for ch, en in seen if int(en))


def measure(ser: serial.Serial, expected: set) -> bytes:
    """Send MEA and collect the response until every expected channel is in."""
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
            if expected:
                got = parse(bytes(buf))
                if all(len(got.get(c, {}).get("values", ())) >= 2 for c in expected):
                    break
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


_last_status = {}


def enrich(chans: dict, sensors: dict) -> None:
    """Attach engineering units to every channel that has a calibration."""
    for ch, data in chans.items():
        sensor = sensors.get(ch)
        if sensor is None:
            continue
        before = sensor.resolved_unit
        eng = sensor.convert(data["values"].get("t01"), data["values"].get("t06"))
        data["eng"] = eng
        if sensor.resolved_unit and sensor.resolved_unit != before:
            log.info("ch %02d: TYPE 01 reads as %s -> %.2f Hz",
                     ch, sensor.resolved_unit, eng.get("frequency_hz", float("nan")))
        if _last_status.get(ch) != eng["status"]:
            _last_status[ch] = eng["status"]
            level = log.info if eng["status"] == "ok" else log.warning
            level("ch %02d (%s): %s%s", ch, sensor.name, eng["status"],
                  " - " + eng["note"] if eng.get("note") else "")


def publish_channel(client, ch: int, payload: dict) -> None:
    base = "%s/ch/%02d" % (TOPIC, ch)
    client.publish(base, json.dumps(payload), qos=0)
    eng = payload.get("eng") or {}
    for key, leaf in (("pressure", "pressure"), ("temperature_c", "temperature"),
                      ("frequency_hz", "frequency"), ("digits", "digits")):
        if eng.get(key) is not None:
            client.publish("%s/%s" % (base, leaf), json.dumps(eng[key]), qos=0)


def main() -> None:
    log.info("ADL-200A -> MQTT | port=%s baud=%d id=%d interval=%ds mqtt=%s:%d topic=%s",
             PORT, BAUD, LOGGER_ID, POLL_INTERVAL, MQTT_HOST, MQTT_PORT, TOPIC)
    sensors = calibration.load_sensors(SENSORS_FILE)
    if sensors:
        log.info("Calibration from %s: %s", SENSORS_FILE, ", ".join(
            "ch%02d=%s" % (c, sensors[c].name) for c in sorted(sensors)))
    else:
        log.info("No calibration in %s - publishing raw counts only", SENSORS_FILE)
    client = make_mqtt()
    ser = open_serial()
    expected = enabled_channels(ser)
    log.info("Logger sweeps %s", ("channels " + ", ".join(
        "%02d" % c for c in sorted(expected))) if expected else "an unknown set of channels")
    since_probe = 0
    while True:
        start = time.monotonic()
        try:
            if not expected:
                # Не долбить GCC каждый цикл: на занятом приборе это лишние
                # полминуты впустую. Пробуем раз в десять циклов.
                since_probe += 1
                if since_probe >= 10:
                    since_probe = 0
                    expected = enabled_channels(ser)
                    if expected:
                        log.info("Logger sweeps channels %s", ", ".join(
                            "%02d" % c for c in sorted(expected)))
            raw = measure(ser, expected)
            chans = parse(raw)
            if chans:
                enrich(chans, sensors)
                ts = datetime.now(timezone.utc).isoformat()
                for ch in sorted(chans):
                    payload = dict(chans[ch])
                    payload["timestamp"] = ts
                    publish_channel(client, ch, payload)
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
