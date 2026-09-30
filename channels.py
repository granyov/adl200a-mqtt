#!/usr/bin/env python3
"""channels.py — посмотреть и изменить набор опрашиваемых каналов ADL-200A.

MEA разворачивает только те каналы, что включены в конфигурации прибора.
Формат команд взят из вендорского ADL2Pro.exe (.rdata), не угадан:

    >>?1,%d,GCC;                        чтение конфигурации, 16 строк
    >>?1,%d,SCC:%d,%d,%d,%d,%d,%d;      запись одного канала

Шесть полей SCC — те же, что возвращает GCC:

    канал, использовать(0/1), тип(1=VW, 2=аналог), температура(0/1), пар5, пар6

Порт занят службой — инструмент останавливает её сам и возвращает на выходе,
в том числе при ошибке:

    python3 channels.py --show
    python3 channels.py --enable-all --yes

Без --yes ничего не пишется — печатаются только кадры, которые были бы посланы.
Перед любой записью снимается резервная копия (--backup, по умолчанию
/var/lib/adl200a/gcc-backup.txt); вернуть всё как было: --restore ФАЙЛ --yes
"""
import argparse
import os
import re
import subprocess
import sys
import time

import serial

SERVICE = "adl200a"

GCC = re.compile(r"<<!\d+,\d+,GCC:(\d+),(\d+),(\d+),(\d+),(\d+),(\d+);")
DEFAULT_BACKUP = "/var/lib/adl200a/gcc-backup.txt"


def systemctl(*args):
    cmd = ["systemctl"] + list(args) + [SERVICE]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    with open(os.devnull, "w") as null:
        return subprocess.call(cmd, stdout=null, stderr=null) == 0


def drain(ser, quiet=4.0, cap=60.0):
    """Дождаться, пока прибор замолчит.

    С шестнадцатью включёнными каналами развёртка идёт ~26 с, и прибор ещё
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


def talk(ser, cmd, wait=6.0, idle=1.5, expect=None):
    """Послать команду и вернуть только относящиеся к ней строки.

    Прибор отвечает с задержкой и вперемешку с кадрами MES, поэтому ответом
    считаем строки с ожидаемым маркером, остальное отбрасываем.
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
        elif buf and time.time() - last > idle:
            break
    lines = [l.strip().decode("latin1") for l in bytes(buf).split(b"\r") if l.strip()]
    return [l for l in lines if mark in l]


def read_config(ser, tries=3):
    """{канал: (использовать, тип, температура, пар5, пар6)}, строки GCC.

    Конфигурацию принимаем только целиком (все 16 каналов): неполный ответ
    означает, что прибор не успел договорить, а не что каналов меньше.
    """
    for attempt in range(tries):
        drain(ser, quiet=3.0, cap=45.0)
        lines = talk(ser, "GCC", wait=30.0, idle=2.0)
        cfg = {}
        for line in lines:
            m = GCC.search(line)
            if m:
                cfg[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
        if len(cfg) == 16:
            return cfg, lines
        if attempt + 1 < tries:
            print("GCC пришёл неполным (%d из 16), прибор занят — повтор %d/%d..."
                  % (len(cfg), attempt + 2, tries))
    return cfg, lines


def show(cfg):
    print("канал  использовать  тип  температура  пар5  пар6")
    for ch in sorted(cfg):
        use, typ, temp, p5, p6 = cfg[ch]
        print("  %02d   %-12s  %-3s  %-11s  %4d  %4d" % (
            ch, "да" if use else "нет",
            {1: "VW", 2: "анлг"}.get(typ, typ) if use else "—",
            ("да" if temp else "нет") if use else "—", p5, p6))
    on = [ch for ch in sorted(cfg) if cfg[ch][0]]
    print("\nопрашивается: %s" % (", ".join("%02d" % c for c in on) if on else "ничего"))


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


def write_channel(ser, ch, fields, commit):
    cmd = "SCC:%d,%d,%d,%d,%d,%d" % ((ch,) + tuple(fields))
    if not commit:
        print("  (сухой прогон) >>?1,%d,%s;" % (ARGS.id, cmd))
        return None
    reply = talk(ser, cmd, wait=8.0, expect="CK")          # ACK или NAK
    print("  >>?1,%d,%s;   ответ: %s" % (ARGS.id, cmd, "; ".join(reply) or "(молчит)"))
    return reply


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default=os.environ.get("ADL200A_PORT", "/dev/ttyS5"))
    ap.add_argument("--baud", type=int, default=int(os.environ.get("ADL200A_BAUD", "38400")))
    ap.add_argument("--id", type=int, default=int(os.environ.get("ADL200A_ID", "1")))
    ap.add_argument("--show", action="store_true", help="показать конфигурацию и выйти")
    ap.add_argument("--enable-all", action="store_true", help="включить все 16 каналов")
    ap.add_argument("--enable", help="включить перечисленные каналы, например 1,3,5")
    ap.add_argument("--disable", help="выключить перечисленные каналы")
    ap.add_argument("--only", help="оставить включёнными только эти каналы, остальные выключить")
    ap.add_argument("--template", default="1,1,1,20,6",
                    help="поля включённого канала: использовать,тип,температура,пар5,пар6")
    ap.add_argument("--backup", default=DEFAULT_BACKUP, help="куда сохранить копию GCC")
    ap.add_argument("--restore", help="вернуть конфигурацию из файла с копией GCC")
    ap.add_argument("--save", action="store_true",
                    help="послать SVE (сохранить в приборе); проверено не было")
    ap.add_argument("--yes", action="store_true", help="действительно писать в прибор")
    ap.add_argument("--no-service", action="store_true",
                    help="не трогать службу adl200a (порт освободи сам)")
    ARGS = ap.parse_args()

    # Читаем копию ДО того, как трогать службу и порт: если файла нет,
    # незачем останавливать сервис ради заведомо неудачного запуска.
    restore = {}
    if ARGS.restore:
        if not os.path.exists(ARGS.restore):
            raise SystemExit(
                "файла с копией нет: %s\n"
                "Копия появляется только после записи с --yes, а её ещё не было —\n"
                "значит и возвращать нечего. Посмотреть, что в приборе: --show" % ARGS.restore)
        for line in open(ARGS.restore):
            m = GCC.search(line)
            if m:
                restore[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
        if not restore:
            raise SystemExit("в файле %s нет строк GCC" % ARGS.restore)

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
        print("=== сейчас в приборе ===")
        show(cfg)

        if ARGS.show:
            return

        targets = dict(restore)
        if not ARGS.restore:
            tmpl = tuple(int(x) for x in ARGS.template.split(","))
            if len(tmpl) != 5:
                raise SystemExit("--template ожидает 5 чисел")
            chans = set()
            if ARGS.only:
                keep = set(int(x) for x in ARGS.only.split(","))
                for ch in range(1, 17):
                    targets[ch] = tmpl if ch in keep else (0, 0, 0, 0, 0)
            elif ARGS.enable_all:
                chans = set(range(1, 17))
            elif ARGS.enable:
                chans = set(int(x) for x in ARGS.enable.split(","))
            for ch in sorted(chans):
                targets[ch] = tmpl
            for ch in (int(x) for x in ARGS.disable.split(",")) if ARGS.disable else ():
                targets[ch] = (0, 0, 0, 0, 0)

        if not targets:
            raise SystemExit("нечего менять: укажи --only, --enable-all, --enable, "
                             "--disable или --restore")

        todo = dict((ch, f) for ch, f in targets.items() if cfg.get(ch) != f)
        if not todo:
            print("\nвсё уже в нужном состоянии, писать нечего")
            return

        saved = None
        if ARGS.yes and not ARGS.restore:
            saved = save_backup(ARGS.backup, lines)
            print("\nрезервная копия: %s  (вернуть: --restore %s --yes)" % (saved, saved))

        print("\n=== запись (%d каналов) ===" % len(todo))
        for ch in sorted(todo):
            write_channel(ser, ch, todo[ch], ARGS.yes)
            if ARGS.yes:
                time.sleep(0.5)          # не частить: прибор отвечает не мгновенно

        if not ARGS.yes:
            print("\nсухой прогон — в прибор ничего не послано. Добавь --yes, чтобы записать.")
            return

        if ARGS.save:
            print("\nSVE: %s" % ("; ".join(talk(ser, "SVE")) or "(молчит)"))

        print("\n=== стало ===")
        again, _ = read_config(ser)
        if len(again) != 16:
            print("проверить не удалось: прибор вернул %d каналов из 16 — он занят\n"
                  "развёрткой. Это НЕ значит, что запись не прошла; посмотри позже:\n"
                  "  python3 %s --show" % (len(again), sys.argv[0]))
            return
        show(again)
        bad = [ch for ch, f in todo.items() if again.get(ch) != f]
        if bad:
            print("\nНЕ ПРИМЕНИЛОСЬ на каналах: %s" %
                  ", ".join("%02d" % c for c in sorted(bad)))
            if saved:
                print("вернуть как было: python3 %s --restore %s --yes" % (sys.argv[0], saved))
    finally:
        ser.close()
        if stopped:
            print("\nвозвращаю службу %s..." % SERVICE)
            if not systemctl("start"):
                print("ВНИМАНИЕ: служба не поднялась, запусти вручную: "
                      "sudo systemctl start %s" % SERVICE)


if __name__ == "__main__":
    main()
