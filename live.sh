#!/bin/bash
# live.sh — живая консольная сессия ADL-200A: сырые отсчёты и инженерные величины
# в реальном времени. Для настройки и установки датчиков.
#
#   ./live.sh                 читать из MQTT (службу не трогает)
#   ./live.sh -d              опрашивать логгер напрямую (быстрее, служба на паузе)
#   ./live.sh -d -z -r        + тарирование по первому отсчёту + сырые кадры протокола
#
# Ctrl-C — выход; в конце печатается сводка по каждому каналу.
set -u

PREFIX="${ADL200A_PREFIX:-/opt/adl200a}"
[ -f "$PREFIX/calibration.py" ] || PREFIX="$(cd "$(dirname "$0")" && pwd)"
[ -f /etc/default/adl200a ] && . /etc/default/adl200a

MODE=mqtt
SENSORS="${ADL200A_SENSORS:-/etc/adl200a/sensors.json}"
MQTT_HOST="${ADL200A_MQTT_HOST:-127.0.0.1}"
MQTT_PORT="${ADL200A_MQTT_PORT:-1883}"
TOPIC="${ADL200A_TOPIC:-adl200a}"
PORT="${ADL200A_PORT:-/dev/ttyUSB0}"
ID="${ADL200A_ID:-1}"
GAP=0
ZERO=""
RAW=""
CHANNEL=""
LOGFILE=""
LIMIT=

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'USAGE'

  -d, --direct        опрашивать логгер напрямую (нужен sudo: служба на паузе)
  -m, --mqtt          читать из MQTT (по умолчанию)
  -c, --channel N     показывать только канал N
  -z, --zero          тарировать: показывать Δ от первого отсчёта
  -r, --raw           показывать сырые кадры протокола
  -g, --gap N         пауза между циклами прямого опроса, с (по умолчанию 0)
  -n, --limit N       остановиться после N строк и напечатать сводку
  -l, --log FILE      писать сессию в файл (без цвета)
  -H, --host HOST     MQTT-брокер (по умолчанию из /etc/default/adl200a)
  -s, --sensors FILE  файл калибровок
  -h, --help          эта справка
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--direct)  MODE=direct ;;
        -m|--mqtt)    MODE=mqtt ;;
        -c|--channel) CHANNEL="$2"; shift ;;
        -z|--zero)    ZERO=1 ;;
        -r|--raw)     RAW=1 ;;
        -g|--gap)     GAP="$2"; shift ;;
        -n|--limit)   LIMIT="$2"; shift ;;
        -l|--log)     LOGFILE="$2"; shift ;;
        -H|--host)    MQTT_HOST="$2"; shift ;;
        -s|--sensors) SENSORS="$2"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# Вывод в конвейер по умолчанию буферизуется поблочно (4 КБ) — строки копились
# бы минутами. stdbuf для mosquitto_sub и python3 -u для своих скриптов.
STDBUF=""
command -v stdbuf >/dev/null && STDBUF="stdbuf -oL"

PYHELPER=$(mktemp "${TMPDIR:-/tmp}/adl200a-live.XXXXXX.py")
PYPOLLER=$(mktemp "${TMPDIR:-/tmp}/adl200a-poll.XXXXXX.py")
WAS_ACTIVE=0
cleanup() {
    rm -f "$PYHELPER" "$PYPOLLER"
    if [ "$WAS_ACTIVE" = 1 ]; then
        echo
        echo "возвращаю службу adl200a..." >&2
        $SUDO systemctl start adl200a
    fi
}
trap cleanup EXIT INT TERM

cat > "$PYHELPER" <<'PYEOF'
"""Рендер живой сессии ADL-200A: кадры протокола или MQTT JSON -> таблица."""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.environ.get("ADL200A_PREFIX", "/opt/adl200a"))
import calibration

MES = re.compile(r"MES:(\d+),(\d+),([0-9/]+),([0-9:]+),(\d+);")

C = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
     "ok": "\033[32m", "warn": "\033[33m", "bad": "\033[31m", "head": "\033[36m"}
STATUS_COLOR = {"ok": "ok", "no_temperature": "warn", "unresolved_scale": "warn",
                "no_vw_signal": "bad", "disconnected": "bad", "raw": "dim"}
STATUS_RU = {"ok": "ok", "no_temperature": "нет темп.", "unresolved_scale": "масштаб?",
             "no_vw_signal": "нет струны", "disconnected": "обрыв",
             "raw": "сырые (нет калибровки)"}


class Session(object):
    def __init__(self, args):
        self.args = args
        self.sensors = calibration.load_sensors(args.sensors)
        self.color = sys.stdout.isatty()
        self.log = open(args.log, "a") if args.log else None
        self.pending = {}
        self.zero = {}
        self.stats = {}
        self.rows = 0
        self.printed = 0
        self.shown_since_cycle = 0

    def paint(self, text, name):
        if not self.color or not name:
            return text
        return C[name] + text + C["reset"]

    def emit(self, plain, colored=None):
        sys.stdout.write((colored if (colored and self.color) else plain) + "\n")
        sys.stdout.flush()
        if self.log:
            self.log.write(plain + "\n")
            self.log.flush()

    # ---------- шапка ----------

    def banner(self, source):
        self.emit("")
        self.emit(self.paint("ADL-200A — живая сессия", "bold"))
        self.emit("источник: %s" % source)
        if self.sensors:
            for ch in sorted(self.sensors):
                s = self.sensors[ch]
                self.emit("калибровка ch%02d: %s (%s s/n %s, диапазон %s %s)" % (
                    ch, s.name, s.model, s.serial, s.full_scale, s.unit))
        else:
            self.emit("калибровки нет (%s) — только сырые отсчёты" % self.args.sensors)
        if self.args.zero:
            self.emit("тарирование: Δ считается от первого годного отсчёта канала")
        self.emit("Ctrl-C — выход и сводка")
        self.header()

    def columns(self):
        cols = [("время", 8), ("ch", 2), ("t01", 9), ("t06", 8),
                ("Гц", 8), ("digits", 9), ("кгс/см²", 10)]
        if self.args.zero:
            cols.append(("Δ", 9))
        cols += [("кПа", 8), ("%FSR", 7), ("°C", 7)]
        return cols

    def header(self):
        cols = self.columns()
        head = "  ".join(n.rjust(w) for n, w in cols) + "  статус"
        rule = "  ".join("-" * w for _, w in cols) + "  ------"
        self.emit("")
        self.emit(head, self.paint(head, "head"))
        self.emit(rule, self.paint(rule, "dim"))
        self.rows = 0

    # ---------- разбор ввода ----------

    def feed(self, line):
        line = line.strip()
        if not line:
            return
        if self.args.raw:
            self.emit("  " + line, self.paint("  " + line, "dim"))
        if line.startswith("---INFO---"):
            msg = line[len("---INFO---"):].strip()
            self.emit(msg, self.paint(msg, "dim"))
            return
        if line.startswith("---CYCLE---"):
            self.flush()
            if not self.shown_since_cycle:
                msg = "%s  — логгер не ответил" % time.strftime("%H:%M:%S")
                self.emit(msg, self.paint(msg, "dim"))
            self.shown_since_cycle = 0
            return
        if line.startswith("{"):
            self.from_json(line)
            return
        for ch, typ, _date, tm, val in MES.findall(line):
            self.add(int(ch), int(typ), int(val), tm)

    def from_json(self, line):
        try:
            doc = json.loads(line)
        except ValueError:
            return
        ch = doc.get("channel")
        vals = doc.get("values") or {}
        if ch is None:
            return
        self.show(int(ch), vals.get("t01"), vals.get("t06"),
                  doc.get("dev_time", time.strftime("%H:%M:%S")))

    def add(self, ch, typ, val, tm):
        slot = self.pending.setdefault(ch, {"time": tm})
        key = "t%02d" % typ
        if key in slot:
            self.flush_channel(ch)
            slot = self.pending.setdefault(ch, {"time": tm})
        slot[key] = val
        slot["time"] = tm

    def flush_channel(self, ch):
        slot = self.pending.pop(ch, None)
        if slot:
            self.show(ch, slot.get("t01"), slot.get("t06"), slot["time"])

    def flush(self):
        for ch in sorted(self.pending):
            self.flush_channel(ch)

    # ---------- вывод строки ----------

    def show(self, ch, t01, t06, dev_time):
        if self.args.channel and ch != self.args.channel:
            return
        sensor = self.sensors.get(ch)
        if sensor is not None:
            eng = sensor.convert(t01, t06)
        else:
            # калибровки нет, но обрыв виден и по сырым числам
            temp = calibration.resistance_to_celsius(t06 or None)
            if not t01:
                status = "disconnected" if temp is None else "no_vw_signal"
            else:
                status = "raw"
            eng = {"status": status, "thermistor_ohms": t06}
            if temp is not None:
                eng["temperature_c"] = round(temp, 2)

        pressure = eng.get("pressure")
        delta = None
        if pressure is not None:
            st = self.stats.setdefault(ch, {"n": 0, "min": pressure, "max": pressure,
                                            "sum": 0.0, "first": pressure})
            st["n"] += 1
            st["sum"] += pressure
            st["min"] = min(st["min"], pressure)
            st["max"] = max(st["max"], pressure)
            if self.args.zero:
                delta = pressure - self.zero.setdefault(ch, pressure)

        def num(v, fmt):
            return ("%" + fmt) % v if v is not None else "—"

        cells = [time.strftime("%H:%M:%S"), "%02d" % ch,
                 num(t01, "d"), num(t06, "d"),
                 num(eng.get("frequency_hz"), ".2f"), num(eng.get("digits"), ".1f"),
                 num(pressure, ".4f")]
        if self.args.zero:
            cells.append(("%+.4f" % delta) if delta is not None else "—")
        cells += [num(eng.get("pressure_kpa"), ".2f"),
                  (("%.2f%%" % eng["percent_fsr"]) if eng.get("percent_fsr") is not None else "—"),
                  num(eng.get("temperature_c"), ".2f")]

        widths = [w for _, w in self.columns()]
        plain = "  ".join(c.rjust(w) for c, w in zip(cells, widths))
        status = eng.get("status", "raw")
        label = STATUS_RU.get(status, status)
        note = eng.get("note")
        line = plain + "  " + label + ("  (%s)" % note if note else "")
        colored = plain + "  " + self.paint(label, STATUS_COLOR.get(status)) \
            + (self.paint("  (%s)" % note, "dim") if note else "")
        self.emit(line, colored)

        self.rows += 1
        self.shown_since_cycle += 1
        self.printed += 1
        if self.args.limit and self.printed >= self.args.limit:
            raise SystemExit(0)
        if self.rows >= 24:
            self.header()

    # ---------- сводка ----------

    def summary(self):
        self.flush()
        if not self.stats:
            self.emit("")
            self.emit("годных отсчётов не было")
            return
        self.emit("")
        self.emit(self.paint("сводка за сессию", "bold"))
        for ch in sorted(self.stats):
            st = self.stats[ch]
            unit = self.sensors[ch].unit if ch in self.sensors else ""
            self.emit("  ch%02d  отсчётов %d   мин %.4f   макс %.4f   среднее %.4f   "
                      "размах %.4f   дрейф от первого %+.4f %s"
                      % (ch, st["n"], st["min"], st["max"], st["sum"] / st["n"],
                         st["max"] - st["min"], st["sum"] / st["n"] - st["first"], unit))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensors", default="/etc/adl200a/sensors.json")
    ap.add_argument("--source", default="?")
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--zero", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--log")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ses = Session(args)
    ses.banner(args.source)
    try:
        for line in sys.stdin:
            ses.feed(line)
    except (KeyboardInterrupt, SystemExit):
        pass
    ses.summary()


if __name__ == "__main__":
    main()
PYEOF

cat > "$PYPOLLER" <<'PYEOF'
"""Прямой опрос ADL-200A: GCC один раз, дальше MEA по кругу.

Ответ на MEA приходит канал за каналом с паузами в несколько секунд, поэтому
"давно нет данных" — плохой признак конца развёртки: он режет её пополам, и
остаток всплывает в следующем цикле. Спрашиваем у прибора, какие каналы
включены, и ждём ровно их.
"""
import argparse
import re
import sys
import time

import serial

MES = re.compile(rb"<<!\d+,\d+,MES:(\d+),(\d+),[0-9/]+,[0-9:]+,\d+;")
GCC = re.compile(rb"<<!\d+,\d+,GCC:(\d+),(\d+),")

ap = argparse.ArgumentParser()
ap.add_argument("--port", required=True)
ap.add_argument("--baud", type=int, default=38400)
ap.add_argument("--id", type=int, default=1)
ap.add_argument("--gap", type=float, default=0.0)
ap.add_argument("--cap", type=float, default=45.0)
ap.add_argument("--idle", type=float, default=6.0)
args = ap.parse_args()

ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0.5)


def ask(cmd):
    ser.reset_input_buffer()
    ser.write((">>?1,%d,%s;\r" % (args.id, cmd)).encode())
    ser.flush()


def drain(buf, emitted):
    """Выдать завершённые строки из буфера, вернуть новую позицию."""
    while True:
        i = buf.find(b"\r", emitted)
        if i < 0:
            return emitted
        line = bytes(buf[emitted:i]).strip()
        emitted = i + 1
        if line:
            sys.stdout.write(line.decode("latin1") + "\n")
            sys.stdout.flush()


def collect(cmd, done, cap, idle):
    ask(cmd)
    buf = bytearray()
    emitted = 0
    t0 = last = time.time()
    while time.time() - t0 < cap:
        d = ser.read(256)
        if d:
            buf += d
            last = time.time()
            emitted = drain(buf, emitted)
            if done(bytes(buf)):
                break
        elif buf and time.time() - last > idle:
            break
    drain(buf, emitted)
    return bytes(buf)


raw = collect("GCC", lambda b: len(GCC.findall(b)) >= 16, 12.0, 1.0)
expected = set(int(m.group(1)) for m in GCC.finditer(raw) if int(m.group(2)))
sys.stdout.write("---INFO--- прибор опрашивает %s\n" % (
    "каналы " + ", ".join("%02d" % c for c in sorted(expected)) if expected
    else "неизвестно какие каналы (GCC не ответил)"))
sys.stdout.flush()


def swept(b):
    if not expected:
        return False
    seen = {}
    for m in MES.finditer(b):
        seen.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    return all(len(seen.get(c, ())) >= 2 for c in expected)


try:
    while True:
        collect("MEA", swept, args.cap, args.idle)
        sys.stdout.write("---CYCLE---\n")
        sys.stdout.flush()
        if args.gap:
            time.sleep(args.gap)
except (KeyboardInterrupt, IOError):
    pass
finally:
    ser.close()
PYEOF

RENDER=(python3 -u "$PYHELPER" --sensors "$SENSORS")
[ -n "$CHANNEL" ] && RENDER+=(--channel "$CHANNEL")
[ -n "$ZERO" ]    && RENDER+=(--zero)
[ -n "$RAW" ]     && RENDER+=(--raw)
[ -n "$LOGFILE" ] && RENDER+=(--log "$LOGFILE")
[ -n "$LIMIT" ]   && RENDER+=(--limit "$LIMIT")
export ADL200A_PREFIX="$PREFIX"

if [ "$MODE" = direct ]; then
    SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    if systemctl is-active --quiet adl200a 2>/dev/null; then
        WAS_ACTIVE=1
        echo "ставлю службу adl200a на паузу на время сессии..." >&2
        $SUDO systemctl stop adl200a || { echo "не смог остановить службу" >&2; exit 1; }
    fi
    RENDER+=(--source "прямой опрос $PORT @${ADL200A_BAUD:-38400} id=$ID")
    python3 -u "$PYPOLLER" --port "$PORT" --baud "${ADL200A_BAUD:-38400}" \
        --id "$ID" --gap "$GAP" | "${RENDER[@]}"
else
    command -v mosquitto_sub >/dev/null || { echo "нет mosquitto_sub (apt install mosquitto-clients)" >&2; exit 1; }
    RENDER+=(--source "MQTT $MQTT_HOST:$MQTT_PORT, топик $TOPIC/ch/+")
    $STDBUF mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "$TOPIC/ch/+" | "${RENDER[@]}"
fi
