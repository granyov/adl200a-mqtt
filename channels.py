#!/usr/bin/env python3
"""channels.py — настройка каналов и собственной записи ADL-200A.

Желаемое состояние описано в том же файле, что и калибровки
(/etc/adl200a/sensors.json): у каждого канала поля "sweep" и "analog", у
прибора — необязательный блок "logger": {"recording": {...}}. Ключ --apply
приводит прибор к файлу:

    python3 channels.py --show              # что сейчас в приборе
    python3 channels.py --apply             # сухой прогон: что поменяется
    python3 channels.py --apply --yes       # записать

Каналы, которых в файле нет, при --apply выключаются (--keep-others — не
трогать): каждый лишний канал удлиняет развёртку примерно на 5 секунд.

Ручная настройка — например, найти, к какому каналу подключён датчик:

    python3 channels.py --enable-all --sweep 3 --yes
    python3 channels.py --only 1 --sweep 3 --analog thermistor_3k --yes

Смысл полей SCC, полосы Sweep и коды типов разобраны из вендорского
ADL2Pro.exe — см. PROTOCOL.md и ace_config.py. Порт занят службой —
инструмент останавливает её сам и возвращает на выходе, в том числе при
ошибке. Без --yes ничего не пишется. Перед записью снимается копия
конфигурации (--backup, по умолчанию /var/lib/adl200a/gcc-backup.txt);
вернуть как было: --restore ФАЙЛ --yes
"""
import argparse
import os
import subprocess
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ace_config as ace

SERVICE = "adl200a"
DEFAULT_BACKUP = "/var/lib/adl200a/gcc-backup.txt"
DEFAULT_SENSORS = os.environ.get("ADL200A_SENSORS", "/etc/adl200a/sensors.json")
SECONDS_PER_CHANNEL = 5      # по замерам на живом приборе: 2 канала ≈ 6 с, 13 ≈ 66–71 с


def systemctl(*args):
    cmd = ["systemctl"] + list(args) + [SERVICE]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        with open(os.devnull, "w") as null:
            return subprocess.call(cmd, stdout=null, stderr=null) == 0
    except OSError:              # нет systemctl или sudo — службы тоже нет
        return False


def drain(ser, quiet=4.0, cap=60.0):
    """Дождаться, пока прибор замолчит.

    С многими включёнными каналами развёртка идёт больше минуты, и прибор ещё
    долго досылает её хвост. Писать конфигурацию поверх этого нельзя: ответы
    уезжают не к тем командам.
    """
    buf = bytearray()
    t0 = last = time.time()
    while time.time() - t0 < cap:
        d = ser.read(256)
        if d:
            buf += d
            last = time.time()
        elif time.time() - last > quiet:
            return len(buf), True
    return len(buf), False


def talk(ser, cmd, wait=6.0, idle=1.5, expect=None, want=0):
    """Послать команду и вернуть только относящиеся к ней строки.

    Прибор отвечает с задержкой и вперемешку с кадрами MES, поэтому ответом
    считаем строки с ожидаемым маркером, остальное отбрасываем. want — сколько
    таких строк ждать: как только пришли все, не ждём тишины.
    """
    mark = expect or cmd.split(":")[0]
    ser.reset_input_buffer()
    ser.write((">>?1,%d,%s;\r" % (ARGS.id, cmd)).encode())
    ser.flush()
    buf = bytearray()
    t0 = last = time.time()
    while time.time() - t0 < wait:
        d = ser.read(256)
        if d:
            buf += d
            last = time.time()
            if want:
                done = [l for l in bytes(buf).split(b"\r")[:-1] if mark.encode() in l]
                if len(done) >= want:
                    break
        elif buf and time.time() - last > idle:
            break
    lines = [l.strip().decode("latin1") for l in bytes(buf).split(b"\r") if l.strip()]
    return [l for l in lines if mark in l]


def read_config(ser, tries=3):
    """{канал: (f2..f6)}, строки GCC.

    Конфигурацию принимаем только целиком (все 16 каналов): неполный ответ
    означает, что прибор не успел договорить, а не что каналов меньше.
    """
    cfg, lines = {}, []
    for attempt in range(tries):
        drain(ser, quiet=3.0, cap=45.0)
        lines = talk(ser, "GCC", wait=30.0, idle=2.0, want=16)
        cfg = ace.parse_gcc(lines)
        if len(cfg) == 16:
            return cfg, lines
        if attempt + 1 < tries:
            print("GCC пришёл неполным (%d из 16), прибор занят — повтор %d/%d..."
                  % (len(cfg), attempt + 2, tries))
    return cfg, lines


def read_recording(ser):
    lines = []
    for cmd in ("GAT", "GAM", "GOV"):
        lines += talk(ser, cmd, wait=10.0, want=1)
    return ace.parse_recording(lines), lines


def sweep_time(cfg):
    n = sum(1 for f in cfg.values() if f[0])
    return n, n * SECONDS_PER_CHANNEL


def show_channels(cfg, plans=None):
    if plans is None:
        print("канал  настройка")
        for ch in sorted(cfg):
            print("  %02d   %s" % (ch, ace.describe(cfg[ch])))
    else:
        print("канал  в приборе%s по файлу" % (" " * 38))
        for ch in sorted(cfg):
            now = ace.describe(cfg[ch])
            if ch in plans:
                then = plans[ch].describe()
                mark = "  " if cfg[ch] == plans[ch].fields else "≠ "
            else:
                then = "выключен (нет в файле)" if not ARGS.keep_others else "не трогаем"
                mark = "≠ " if cfg[ch][0] and not ARGS.keep_others else "  "
            print("%s%02d   %-47s %s" % (mark, ch, now, then))
    n, secs = sweep_time(cfg)
    print("\nвключено каналов: %d, развёртка ≈ %d с" % (n, secs))


def parse_channels(text):
    """"1,3,5-8" -> {1, 3, 5, 6, 7, 8}."""
    out = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    bad = sorted(c for c in out if not 1 <= c <= 16)
    if bad:
        raise SystemExit("каналов %s нет, есть 1–16" % ", ".join(str(c) for c in bad))
    return out


def save_backup(path, lines):
    """Копия обязательна: без неё запись отменяем, а не идём наугад."""
    fallback = os.path.join(os.path.expanduser("~"), "adl200a-gcc-backup.txt")
    for cand in (path, fallback):
        try:
            d = os.path.dirname(cand)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with open(cand, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            return cand
        except OSError as e:
            print("копию в %s записать не вышло (%s)" % (cand, e))
    raise SystemExit("резервную копию сохранить не удалось — запись отменена")


def send(ser, cmd, commit):
    if not commit:
        print("  (сухой прогон) >>?1,%d,%s;" % (ARGS.id, cmd))
        return None
    reply = talk(ser, cmd, wait=8.0, expect="CK", want=1)          # ACK или NAK
    print("  >>?1,%d,%s;   ответ: %s" % (ARGS.id, cmd, "; ".join(reply) or "(молчит)"))
    time.sleep(0.5)                              # не частить: прибор отвечает не мгновенно
    return reply


def desired_state():
    """(каналы, запись, планы по файлу) — считаются ДО того, как трогать прибор."""
    want, want_rec, plans = {}, {}, None

    if ARGS.apply:
        plans, rec, problems = ace.load_plan(ARGS.sensors, strict=True)
        if problems:
            raise SystemExit("в %s есть ошибки, прибор не трогаю:\n  - %s"
                             % (ARGS.sensors, "\n  - ".join(problems)))
        if not plans and not rec:
            raise SystemExit("в %s нет ни одного канала с sweep/analog и нет "
                             "logger.recording — применять нечего" % ARGS.sensors)
        for ch in range(1, 17):
            if ch in plans:
                want[ch] = plans[ch].fields
            elif not ARGS.keep_others:
                want[ch] = ace.OFF
        want_rec = rec or {}

    elif ARGS.restore:
        if not os.path.exists(ARGS.restore):
            raise SystemExit(
                "файла с копией нет: %s\n"
                "Копия появляется только после записи с --yes, а её ещё не было —\n"
                "значит и возвращать нечего. Посмотреть, что в приборе: --show" % ARGS.restore)
        text = open(ARGS.restore).read()
        want = ace.parse_gcc(text)
        want_rec = ace.parse_recording(text)
        if not want:
            raise SystemExit("в файле %s нет строк GCC" % ARGS.restore)

    else:
        enabling = ARGS.enable_all or ARGS.enable or ARGS.only
        fields = None
        if enabling:
            if ARGS.sweep is None:
                raise SystemExit("укажи --sweep: 1–4 или 0, если струнный вход не нужен.\n"
                                 "Полосы: %s" % ", ".join(ace.band_label(s)
                                                          for s in sorted(ace.SWEEP_BANDS)))
            analog = 0
            if ARGS.analog not in (None, "none"):
                if ARGS.analog not in ace.ANALOG_TYPES:
                    raise SystemExit("--analog: %s или none"
                                     % ", ".join(sorted(ace.ANALOG_TYPES)))
                analog = ace.ANALOG_TYPES[ARGS.analog]
            try:
                fields = ace.build_fields(ARGS.sweep, analog, ARGS.serial_count)
            except ace.ConfigError as e:
                raise SystemExit(str(e))
            if fields == ace.OFF:
                raise SystemExit("и струнный, и аналоговый вход выключены — это --disable")
        if ARGS.only:
            keep = parse_channels(ARGS.only)
            for ch in range(1, 17):
                want[ch] = fields if ch in keep else ace.OFF
        elif ARGS.enable_all:
            want = dict((ch, fields) for ch in range(1, 17))
        elif ARGS.enable:
            want = dict((ch, fields) for ch in parse_channels(ARGS.enable))
        if ARGS.disable:
            for ch in parse_channels(ARGS.disable):
                want[ch] = ace.OFF
    return want, want_rec, plans


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default=os.environ.get("ADL200A_PORT", "/dev/ttyS5"))
    ap.add_argument("--baud", type=int, default=int(os.environ.get("ADL200A_BAUD", "38400")))
    ap.add_argument("--id", type=int, default=int(os.environ.get("ADL200A_ID", "1")))
    ap.add_argument("--sensors", default=DEFAULT_SENSORS, help="файл с конфигурацией каналов")
    ap.add_argument("--show", action="store_true", help="показать, что в приборе, и выйти")
    ap.add_argument("--apply", action="store_true", help="привести прибор к файлу --sensors")
    ap.add_argument("--keep-others", action="store_true",
                    help="при --apply не выключать каналы, которых нет в файле")
    ap.add_argument("--enable-all", action="store_true", help="включить все 16 каналов")
    ap.add_argument("--enable", help="включить каналы, например 1,3,5-8")
    ap.add_argument("--only", help="оставить включёнными только эти каналы")
    ap.add_argument("--disable", help="выключить каналы")
    ap.add_argument("--sweep", type=int, choices=[0, 1, 2, 3, 4],
                    help="полоса струнного входа при ручной настройке, 0 — выкл.")
    ap.add_argument("--analog", default="thermistor_3k",
                    help="аналоговый вход при ручной настройке: %s или none"
                         % ", ".join(sorted(ace.ANALOG_TYPES)))
    ap.add_argument("--serial-count", type=int, help="число значений serial-датчика")
    ap.add_argument("--backup", default=DEFAULT_BACKUP, help="куда сохранить копию конфигурации")
    ap.add_argument("--restore", help="вернуть конфигурацию из файла с копией")
    ap.add_argument("--save", action="store_true",
                    help="послать SVE (сохранить в приборе); проверено не было")
    ap.add_argument("--yes", action="store_true", help="действительно писать в прибор")
    ap.add_argument("--no-service", action="store_true",
                    help="не трогать службу adl200a (порт освободи сам)")
    ARGS = ap.parse_args()

    manual = bool(ARGS.enable_all or ARGS.enable or ARGS.only or ARGS.disable)
    if sum(bool(x) for x in (ARGS.show, ARGS.apply, ARGS.restore, manual)) != 1:
        ap.error("выбери одно: --show, --apply, --restore или ручную настройку "
                 "(--enable-all / --enable / --only / --disable)")

    want, want_rec, plans = desired_state()

    stopped = False
    if not ARGS.no_service and systemctl("is-active", "--quiet"):
        print("останавливаю службу %s на время работы..." % SERVICE)
        if not systemctl("stop"):
            raise SystemExit("не смог остановить службу — порт останется занят")
        stopped = True

    try:
        ser = serial.Serial(ARGS.port, ARGS.baud, bytesize=8,
                            parity="N", stopbits=1, timeout=0.5)
    except serial.SerialException as e:
        if stopped:
            systemctl("start")
        raise SystemExit("порт %s не открылся: %s" % (ARGS.port, e))

    try:
        cfg, lines = read_config(ser)
        if len(cfg) != 16:
            raise SystemExit(
                "не удалось прочитать конфигурацию целиком (%d каналов из 16).\n"
                "Прибор занят развёрткой — чем больше включено каналов, тем дольше\n"
                "он отвечает на служебные команды. Повтори через минуту." % len(cfg))
        rec_now, rec_lines = {}, []
        if ARGS.show or want_rec:
            rec_now, rec_lines = read_recording(ser)

        print("=== сейчас в приборе ===")
        show_channels(cfg, plans)
        if ARGS.show or want_rec:
            print("собственная запись: %s" % ace.describe_recording(rec_now))
        if ARGS.show:
            return

        todo = dict((ch, f) for ch, f in want.items() if cfg.get(ch) != f)
        rec_todo = [(get, cmd) for get, cmd in ace.recording_commands(want_rec)
                    if rec_now.get(get) != want_rec[get]]
        if not todo and not rec_todo:
            print("\nвсё уже как надо, писать нечего")
            return

        after = dict(cfg)
        after.update(todo)
        n, secs = sweep_time(after)
        print("\n=== что поменяется ===")
        for ch in sorted(todo):
            print("  %02d: %s  →  %s" % (ch, ace.describe(cfg[ch]), ace.describe(todo[ch])))
        if rec_todo:
            print("  запись: %s  →  %s" % (ace.describe_recording(rec_now),
                                           ace.describe_recording(want_rec)))
        print("  после: включено каналов %d, развёртка ≈ %d с" % (n, secs))

        saved = None
        if ARGS.yes and not ARGS.restore:
            saved = save_backup(ARGS.backup, lines + rec_lines)
            print("\nрезервная копия: %s  (вернуть: --restore %s --yes)" % (saved, saved))

        print("\n=== запись ===")
        for ch in sorted(todo):
            send(ser, ace.scc_command(ch, todo[ch]), ARGS.yes)
        for _get, cmd in rec_todo:
            send(ser, cmd, ARGS.yes)

        if not ARGS.yes:
            print("\nсухой прогон — в прибор ничего не послано. Добавь --yes, чтобы записать.")
            return

        if ARGS.save:
            print("\nSVE: %s" % ("; ".join(talk(ser, "SVE", expect="CK", want=1)) or "(молчит)"))

        print("\n=== проверка ===")
        again, _ = read_config(ser)
        if len(again) != 16:
            print("проверить не удалось: прибор вернул %d каналов из 16 — он занят\n"
                  "развёрткой. Это НЕ значит, что запись не прошла; посмотри позже:\n"
                  "  python3 %s --show" % (len(again), sys.argv[0]))
            return
        bad = [ch for ch, f in todo.items() if again.get(ch) != f]
        if rec_todo:
            rec_again, _ = read_recording(ser)
            bad_rec = [get for get, _cmd in rec_todo if rec_again.get(get) != want_rec[get]]
        else:
            bad_rec = []
        show_channels(again, plans)
        if bad or bad_rec:
            if bad:
                print("\nНЕ ПРИМЕНИЛОСЬ на каналах: %s" %
                      ", ".join("%02d" % c for c in sorted(bad)))
            if bad_rec:
                print("\nНЕ ПРИМЕНИЛОСЬ в настройках записи: %s" % ", ".join(bad_rec))
            if saved:
                print("вернуть как было: python3 %s --restore %s --yes" % (sys.argv[0], saved))
        else:
            print("\nвсё применилось")
    finally:
        ser.close()
        if stopped:
            print("\nвозвращаю службу %s..." % SERVICE)
            if not systemctl("start"):
                print("ВНИМАНИЕ: служба не поднялась, запусти вручную: "
                      "sudo systemctl start %s" % SERVICE)


if __name__ == "__main__":
    main()
