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
  ADL200A_INTERVAL (100)       ADL200A_MQTT_HOST (127.0.0.1)
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
import socket
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import serial
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibration
from delivery import Outbox, wait_connected

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))
LOGGER_ID = int(os.environ.get("ADL200A_ID", "1"))
POLL_INTERVAL = int(os.environ.get("ADL200A_INTERVAL", "100"))
MQTT_HOST = os.environ.get("ADL200A_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("ADL200A_MQTT_PORT", "1883"))
TOPIC = os.environ.get("ADL200A_TOPIC", "adl200a")
SENSORS_FILE = os.environ.get("ADL200A_SENSORS", "/etc/adl200a/sensors.json")
PENDING_FILE = os.environ.get("ADL200A_PENDING_FILE", "/var/lib/adl200a/pending.json")
CLIENT_ID = os.environ.get("ADL200A_MQTT_CLIENT_ID",
                           "adl200a-%s-%d" % (socket.gethostname(), LOGGER_ID))

MEA_MAX_SECONDS = int(os.environ.get("ADL200A_MEA_MAX_SECONDS", "90"))
MEA_IDLE_DONE = float(os.environ.get("ADL200A_MEA_IDLE_DONE", "20"))
READ_TIMEOUT = 0.5

MES_RE = re.compile(rb"<<!(\d+),(\d+),MES:(\d+),(\d+),([0-9/]+),([0-9:]+),(\d+);")
GCC_RE = re.compile(rb"<<!\d+,(\d+),GCC:(\d+),([01]),\d+,\d+,\d+,\d+;")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("adl200a")


def make_mqtt() -> mqtt.Client:
    try:  # paho-mqtt >= 2.0
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)

        def on_connect(cl, u, flags, reason_code, props):
            log.info("MQTT connected: %s", reason_code)

        def on_disconnect(cl, u, flags, reason_code, props):
            log.warning("MQTT disconnected: %s", reason_code)
    except AttributeError:  # paho-mqtt < 2.0
        c = mqtt.Client(client_id=CLIENT_ID)

        def on_connect(cl, u, flags, rc):
            log.info("MQTT connected rc=%s", rc)

        def on_disconnect(cl, u, rc):
            log.warning("MQTT disconnected rc=%s", rc)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.max_inflight_messages_set(1)
    c.max_queued_messages_set(20)
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect_async(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    return c


def open_serial() -> serial.Serial:
    while True:
        try:
            s = serial.Serial(PORT, BAUD, bytesize=serial.EIGHTBITS,
                              parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                              timeout=READ_TIMEOUT, write_timeout=5, exclusive=True)
            log.info("Serial open %s @ %d 8N1", PORT, BAUD)
            return s
        except serial.SerialException as e:
            log.error("Serial open failed: %s (retry 5s)", e)
            time.sleep(5)


def enabled_channels(ser: serial.Serial):
    """Ask GCC which channels the logger actually sweeps.

    A MEA answer arrives channel by channel with seconds of silence in between,
    so "no data for a while" is not a reliable end-of-sweep marker: it truncates
    the sweep and the leftover frames surface in the next cycle. Knowing how many
    channels to expect lets measure() stop exactly when the sweep is complete.
    """
    # Сначала дать прибору договорить: он долго досылает хвост развёртки, и
    # ответ на GCC приедет вперемешку с кадрами MES.
    t0 = last = time.monotonic()
    while time.monotonic() - t0 < 45:
        if ser.read(1024):
            last = time.monotonic()
        elif time.monotonic() - last > 3.0:
            break

    ser.reset_input_buffer()
    ser.write((">>?1,%d,GCC;\r" % LOGGER_ID).encode())
    ser.flush()
    buf = bytearray()
    t0 = time.monotonic()
    cfg = {}
    while time.monotonic() - t0 < 30:
        d = ser.read(1024)
        if d:
            buf += d
            cfg = {int(ch): int(en) for addr, ch, en in GCC_RE.findall(bytes(buf))
                   if int(addr) == LOGGER_ID and 1 <= int(ch) <= 16}
            if len(cfg) == 16:
                break
    if len(cfg) < 16:
        # Неполный ответ — это "прибор не договорил", а не "каналов меньше".
        # Лучше ничего не знать, чем знать неправду: measure() тогда уйдёт на
        # запасной порог тишины.
        log.warning("GCC пришёл неполным (%d из 16) — прибор занят", len(cfg))
        return None
    return {ch for ch, enabled in cfg.items() if enabled}


def measure(ser: serial.Serial, expected):
    """Send MEA and collect the response until every expected channel is in."""
    cmd = (">>?1,%d,MEA;\r" % LOGGER_ID).encode()
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()
    buf = bytearray()
    t0 = time.monotonic()
    last = t0
    reason = "timeout"
    while time.monotonic() - t0 < MEA_MAX_SECONDS:
        d = ser.read(1024)
        if d:
            buf += d
            last = time.monotonic()
            if expected is not None:
                got = parse(bytes(buf))
                if (all({"t01", "t06"}.issubset(got.get(c, {}).get("values", {}))
                        for c in expected)
                        and len({(got[c]["dev_date"], got[c]["dev_time"])
                                 for c in expected}) == 1):
                    reason = "complete"
                    break
        elif expected is None and buf and time.monotonic() - last > MEA_IDLE_DONE:
            reason = "idle_unknown_channels"
            break
    return bytes(buf), reason


def parse(buf: bytes) -> dict:
    chans = {}
    for m in MES_RE.finditer(buf):
        _lid, addr, ch, typ, date, tm, val = m.groups()
        ch = int(ch)
        if int(addr) != LOGGER_ID or not 1 <= ch <= 16 or int(typ) not in (1, 6):
            continue
        stamp = (date.decode(), tm.decode())
        c = chans.get(ch)
        if c is None or (c["dev_date"], c["dev_time"]) != stamp:
            c = {"channel": ch, "values": {}, "dev_date": stamp[0], "dev_time": stamp[1]}
            chans[ch] = c
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


def channel_messages(ch: int, payload: dict):
    base = "%s/ch/%02d" % (TOPIC, ch)
    messages = [[base, json.dumps(payload)]]
    eng = payload.get("eng") or {}
    for key, leaf in (("pressure", "pressure"), ("temperature_c", "temperature"),
                      ("frequency_hz", "frequency"), ("digits", "digits")):
        if eng.get(key) is not None:
            messages.append(["%s/%s" % (base, leaf), json.dumps(eng[key])])
    return messages


def main() -> None:
    log.info("ADL-200A -> MQTT | port=%s baud=%d id=%d interval=%ds mqtt=%s:%d topic=%s",
             PORT, BAUD, LOGGER_ID, POLL_INTERVAL, MQTT_HOST, MQTT_PORT, TOPIC)
    sensors = calibration.load_sensors(SENSORS_FILE)
    if sensors:
        log.info("Calibration from %s: %s", SENSORS_FILE, ", ".join(
            "ch%02d=%s" % (c, sensors[c].name) for c in sorted(sensors)))
    else:
        log.info("No calibration in %s - publishing raw counts only", SENSORS_FILE)
    outbox = Outbox(PENDING_FILE)
    outbox.lock()
    client = make_mqtt()
    # Recover an already collected response before sending another ACE command.
    outbox.flush(client)
    wait_connected(client)
    ser = open_serial()
    expected = enabled_channels(ser)
    log.info("Logger sweeps %s", ("channels " + ", ".join(
        "%02d" % c for c in sorted(expected))) if expected else "an unknown set of channels")
    since_probe = 0
    while True:
        start = time.monotonic()
        wait_connected(client)
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
            if expected == set():
                log.warning("Logger has no enabled channels; waiting before GCC retry")
                time.sleep(POLL_INTERVAL)
                continue
            measurement_start = time.monotonic()
            raw, reason = measure(ser, expected)
            chans = parse(raw)
        except serial.SerialException as e:
            log.error("Serial error: %s (reconnecting)", e)
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial()
            expected = enabled_channels(ser)
            continue
        except Exception as e:  # keep the daemon alive
            log.exception("Unexpected: %s", e)
            time.sleep(5)
            continue
        if chans:
            try:
                enrich(chans, sensors)
            except Exception:
                log.exception("Calibration failed; delivering raw response")
                for channel in chans.values():
                    channel.pop("eng", None)
            complete_channels = {c for c, data in chans.items()
                                 if {"t01", "t06"}.issubset(data["values"])}
            missing = sorted(expected - complete_channels) if expected is not None else None
            cycle_id = uuid.uuid4().hex
            meta = {"cycle_id": cycle_id, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "complete": reason == "complete" if expected is not None else None,
                    "expected_channels": sorted(expected) if expected is not None else None,
                    "missing_channels": missing, "end_reason": reason,
                    "consistent_device_time": len({(c["dev_date"], c["dev_time"])
                                                   for c in chans.values()}) == 1,
                    "duration_seconds": round(time.monotonic() - measurement_start, 2)}
            messages = []
            for ch in sorted(chans):
                payload = dict(chans[ch], **meta)
                messages.extend(channel_messages(ch, payload))
            messages.append(["%s/measurement" % TOPIC, json.dumps(
                dict(meta, channels=[chans[c] for c in sorted(chans)]))])
            if meta["complete"] is not True:
                log.warning("Cycle %s incomplete/unverified: missing=%s end=%s",
                            cycle_id, missing, reason)
            # Do not drop the response or collect a new one on storage failure.
            while True:
                try:
                    outbox.stage(cycle_id, messages)
                    break
                except OSError:
                    log.exception("Cannot save pending response; retrying before next command")
                    if os.path.exists(PENDING_FILE):
                        raise
                    time.sleep(5)
            outbox.flush(client)
            log.info("Delivered %d channels (%d raw bytes), complete=%s cycle=%s",
                     len(chans), len(raw), meta["complete"], cycle_id)
        else:
            log.warning("No MES frames parsed (%d bytes) - logger connected on %s?",
                        len(raw), PORT)
        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
