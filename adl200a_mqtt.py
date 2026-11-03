#!/usr/bin/env python3
"""ADL-200A (ACE Instrument) data-logger -> MQTT daemon.

Portable across hosts: all settings come from environment variables, so the same
code runs on a Moxa UPort 1150 (/dev/ttyUSB0) or a native RS-232 port (/dev/ttyS0).

Protocol (ACE ASCII over RS-232, reverse-engineered from ADL2Pro.exe, confirmed
on hardware):
  Request : >>?1,<ID>,<CMD>;\\r          (CR only, no checksum)
  Response: <<!01,<ID>,<CMD>:<data>;\\r
  MEA (channels enabled in GCC, ~5 s each) -> per channel & input type:
     <<!01,<ID>,MES:<CH>,<TYPE>,<yy/mm/dd>,<hh:mm:ss>,<VALUE>;\\r
     TYPE = input type code: 01 VW (Hz x100), 06 thermistor (ohms), ... (ace_config.py)

Env vars (defaults in parentheses):
  ADL200A_PORT (/dev/ttyUSB0)  ADL200A_BAUD (38400)  ADL200A_ID (1)
  ADL200A_INTERVAL (100)       ADL200A_MQTT_HOST (127.0.0.1)
  ADL200A_MQTT_PORT (1883)     ADL200A_TOPIC (adl200a)
  ADL200A_MEA_MAX_SECONDS (90) ADL200A_MEA_IDLE_DONE (20)
  ADL200A_PENDING_FILE (/var/lib/adl200a/pending.json)
  ADL200A_MQTT_CLIENT_ID (adl200a-<hostname>-<logger id>)

Delivery: every cycle is staged on disk and published with QoS 1; the next
serial command waits until the broker has acknowledged all of it (delivery.py).

MQTT carries the logger's raw counts and nothing else — no calibration is applied
here, so the stored series never depends on a calibration file that may later be
corrected. Engineering units are for checking by eye at the console: live.sh and
calibration.py, both reading /etc/adl200a/sensors.json.

What the raw values mean is published retained on <topic>/config: the channel
configuration read from the logger (GCC), decoded — sweep band, analog type and
the meaning of every tNN key per channel.
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
import ace_config
from delivery import Outbox, wait_connected

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))
LOGGER_ID = int(os.environ.get("ADL200A_ID", "1"))
POLL_INTERVAL = int(os.environ.get("ADL200A_INTERVAL", "100"))
MQTT_HOST = os.environ.get("ADL200A_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("ADL200A_MQTT_PORT", "1883"))
TOPIC = os.environ.get("ADL200A_TOPIC", "adl200a")
PENDING_FILE = os.environ.get("ADL200A_PENDING_FILE", "/var/lib/adl200a/pending.json")
CLIENT_ID = os.environ.get("ADL200A_MQTT_CLIENT_ID",
                           "adl200a-%s-%d" % (socket.gethostname(), LOGGER_ID))

MEA_MAX_SECONDS = int(os.environ.get("ADL200A_MEA_MAX_SECONDS", "90"))
MEA_IDLE_DONE = float(os.environ.get("ADL200A_MEA_IDLE_DONE", "20"))
READ_TIMEOUT = 0.5
MIN_GAP = 5.0               # never start a sweep right on top of the previous one
SECONDS_PER_CHANNEL = 6.0   # generous: the logger answers ~1 channel per 5 s

MES_RE = re.compile(rb"<<!(\d+),(\d+),MES:(\d+),(\d+),([0-9/]+),([0-9:]+),(\d+);")
GCC_RE = re.compile(rb"<<!\d+,(\d+),GCC:(\d+),(\d+),(\d+),(\d+),(\d+),(\d+);")

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


def drain(ser: serial.Serial, quiet: float = 3.0, cap: float = 120.0) -> int:
    """Wait until the logger stops talking; return how many stale bytes it sent.

    Anything still arriving belongs to the previous sweep. Starting a new command
    on top of it is what puts the logger behind: the requests queue up, answers
    come out against the wrong command, and each cycle carries leftovers.
    """
    stale = 0
    t0 = last = time.monotonic()
    while time.monotonic() - t0 < cap:
        d = ser.read(1024)
        if d:
            stale += len(d)
            last = time.monotonic()
        elif time.monotonic() - last > quiet:
            break
    return stale


def read_channel_config(ser: serial.Serial):
    """Read GCC: {channel: (f2..f6)} for all 16 channels, or None if incomplete.

    A MEA answer arrives channel by channel with seconds of silence in between,
    so "no data for a while" is not a reliable end-of-sweep marker: it truncates
    the sweep and the leftover frames surface in the next cycle. Knowing which
    channels and which input types to expect lets measure() stop exactly when
    the sweep is complete.
    """
    # Сначала дать прибору договорить: он долго досылает хвост развёртки, и
    # ответ на GCC приедет вперемешку с кадрами MES.
    drain(ser, quiet=3.0, cap=45.0)
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
            cfg = dict((int(m[1]), tuple(int(x) for x in m[2:]))
                       for m in GCC_RE.findall(bytes(buf))
                       if int(m[0]) == LOGGER_ID and 1 <= int(m[1]) <= 16)
            if len(cfg) == 16:
                break
    if len(cfg) < 16:
        # Неполный ответ — это "прибор не договорил", а не "каналов меньше".
        # Лучше ничего не знать, чем знать неправду: measure() тогда уйдёт на
        # запасной порог тишины.
        log.warning("GCC пришёл неполным (%d из 16) — прибор занят", len(cfg))
        return None
    return cfg


def enabled_channels(ser: serial.Serial):
    """Channels the logger sweeps: a set, or None when GCC came incomplete."""
    cfg = read_channel_config(ser)
    if cfg is None:
        return None
    return set(expected_frames(cfg))


def expected_frames(cfg):
    """{channel: {TYPE codes}} the next MEA must deliver, from the GCC config."""
    if cfg is None:
        return None
    out = {}
    for ch, fields in cfg.items():
        types = set(ace_config.frame_types(fields))
        if types:
            out[ch] = types
    return out


def _as_frames(expected):
    """Accept {ch: {types}}, or a plain set of channels (VW + thermistor each)."""
    if expected is None or isinstance(expected, dict):
        return expected
    return dict((ch, {ace_config.TYPE_VW, ace_config.ANALOG_TYPES["thermistor_3k"]})
                for ch in expected)


def publish_config(client, cfg) -> None:
    """Retained, so a consumer that subscribes later still learns what tNN mean."""
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(),
               "logger_id": LOGGER_ID,
               "channels": ace_config.decode(cfg)}
    # qos=1: paho держит такое сообщение в очереди, пока не подключится к брокеру
    client.publish("%s/config" % TOPIC, json.dumps(payload, ensure_ascii=False),
                   qos=1, retain=True)
    for item in payload["channels"]:
        log.info("ch %02d: %s", item["channel"], item["setting"])


def sweep_budget(expected) -> float:
    """How long one sweep may take. Grows with the number of enabled channels."""
    n = len(expected) if expected else 16
    return max(float(MEA_MAX_SECONDS), 20.0 + SECONDS_PER_CHANNEL * n)


def _received(chans: dict, ch: int) -> set:
    return set(int(k[1:]) for k in chans.get(ch, {}).get("values", ()))


def measure(ser: serial.Serial, expected):
    """Send MEA and collect the response until every expected frame is in.

    Returns (raw bytes, reason): "complete" when every expected channel delivered
    every configured input from one and the same sweep, "timeout" otherwise, and
    "idle_unknown_channels" when the configuration was unknown and the logger
    went quiet.
    """
    expected = _as_frames(expected)
    cmd = (">>?1,%d,MEA;\r" % LOGGER_ID).encode()
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()
    buf = bytearray()
    budget = sweep_budget(expected)
    t0 = time.monotonic()
    last = t0
    reason = "timeout"
    while time.monotonic() - t0 < budget:
        d = ser.read(1024)
        if d:
            buf += d
            last = time.monotonic()
            if expected is not None:
                got = parse(bytes(buf))
                if (all(types <= _received(got, c) for c, types in expected.items())
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
        if (int(addr) != LOGGER_ID or not 1 <= ch <= 16
                or int(typ) not in ace_config.TYPE_LABELS):
            continue
        stamp = (date.decode(), tm.decode())
        c = chans.get(ch)
        if c is None or (c["dev_date"], c["dev_time"]) != stamp:
            c = {"channel": ch, "values": {}, "dev_date": stamp[0], "dev_time": stamp[1]}
            chans[ch] = c
        c["values"]["t%02d" % int(typ)] = int(val)
    return chans


def channel_messages(ch: int, payload: dict):
    """One raw message per channel; meanings of tNN are on <topic>/config."""
    return [["%s/ch/%02d" % (TOPIC, ch), json.dumps(payload)]]


def main() -> None:
    log.info("ADL-200A -> MQTT | port=%s baud=%d id=%d interval=%ds mqtt=%s:%d topic=%s",
             PORT, BAUD, LOGGER_ID, POLL_INTERVAL, MQTT_HOST, MQTT_PORT, TOPIC)
    outbox = Outbox(PENDING_FILE)
    outbox.lock()
    client = make_mqtt()
    # Recover an already collected response before sending another ACE command.
    outbox.flush(client)
    wait_connected(client)
    ser = open_serial()
    cfg = read_channel_config(ser)
    expected = expected_frames(cfg)
    if cfg is not None:
        log.info("Logger sweeps %s", ("channels " + ", ".join(
            "%02d" % c for c in sorted(expected))) if expected else "no channels at all")
        publish_config(client, cfg)
    else:
        log.info("Logger sweeps an unknown set of channels")
    since_probe = 0
    warned = False
    while True:
        start = time.monotonic()
        wait_connected(client)
        try:
            stale = drain(ser, quiet=2.0)
            if stale:
                log.warning("Dropped %d stale bytes before MEA - the previous sweep "
                            "had not finished", stale)
            if cfg is None:
                # Не долбить GCC каждый цикл: на занятом приборе это лишние
                # полминуты впустую. Пробуем раз в десять циклов.
                since_probe += 1
                if since_probe >= 10:
                    since_probe = 0
                    cfg = read_channel_config(ser)
                    expected = expected_frames(cfg)
                    if cfg is not None:
                        log.info("Logger sweeps channels %s", ", ".join(
                            "%02d" % c for c in sorted(expected)))
                        publish_config(client, cfg)
            if expected == {}:
                log.warning("Logger has no enabled channels; waiting before GCC retry")
                time.sleep(POLL_INTERVAL)
                cfg = read_channel_config(ser)
                expected = expected_frames(cfg)
                continue
            measurement_start = time.monotonic()
            raw, reason = measure(ser, expected)
            sweep = time.monotonic() - measurement_start
            chans = parse(raw)
        except serial.SerialException as e:
            log.error("Serial error: %s (reconnecting)", e)
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial()
            cfg = read_channel_config(ser)
            expected = expected_frames(cfg)
            continue
        except Exception as e:  # keep the daemon alive
            log.exception("Unexpected: %s", e)
            time.sleep(5)
            continue
        if sweep > POLL_INTERVAL - MIN_GAP and not warned:
            warned = True
            log.warning("A sweep takes %.0f s but ADL200A_INTERVAL is %d s. "
                        "Cycles will run at the logger's pace instead; raise the "
                        "interval or enable fewer channels (channels.py --apply)",
                        sweep, POLL_INTERVAL)
        if chans:
            missing = (sorted(c for c, types in expected.items()
                              if not types <= _received(chans, c))
                       if expected is not None else None)
            cycle_id = uuid.uuid4().hex
            meta = {"cycle_id": cycle_id, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "complete": reason == "complete" if expected is not None else None,
                    "expected_channels": sorted(expected) if expected is not None else None,
                    "missing_channels": missing, "end_reason": reason,
                    "consistent_device_time": len({(c["dev_date"], c["dev_time"])
                                                   for c in chans.values()}) == 1,
                    "duration_seconds": round(sweep, 2)}
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
            log.info("Delivered %d channels (%d raw bytes, sweep %.0f s), complete=%s cycle=%s",
                     len(chans), len(raw), sweep, meta["complete"], cycle_id)
        else:
            log.warning("No MES frames parsed (%d bytes) - logger connected on %s?",
                        len(raw), PORT)
        # MIN_GAP, а не 0: даже если развёртка перебрала интервал, следующий MEA
        # не уходит впритык — иначе прибор снова начинает отставать.
        time.sleep(max(MIN_GAP, POLL_INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
