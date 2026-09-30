#!/usr/bin/env python3
"""channels.py — посмотреть и изменить набор опрашиваемых каналов ADL-200A.

MEA разворачивает только те каналы, что включены в конфигурации прибора.
Формат команд взят из вендорского ADL2Pro.exe (.rdata), не угадан:

    >>?1,%d,GCC;                        чтение конфигурации, 16 строк
    >>?1,%d,SCC:%d,%d,%d,%d,%d,%d;      запись одного канала

Шесть полей SCC — те же, что возвращает GCC:

    канал, использовать(0/1), тип(1=VW, 2=аналог), температура(0/1), пар5, пар6

Порт занят службой, поэтому её надо остановить:

    sudo systemctl stop adl200a
    python3 channels.py --show
    python3 channels.py --enable-all --yes
    sudo systemctl start adl200a

Без --yes ничего не пишется — печатаются только кадры, которые были бы посланы.
Перед любой записью снимается резервная копия (--backup, по умолчанию
/var/lib/adl200a/gcc-backup.txt); вернуть всё как было: --restore ФАЙЛ --yes
"""
import argparse
import os
import re
import sys
import time

import serial

GCC = re.compile(r"<<!\d+,\d+,GCC:(\d+),(\d+),(\d+),(\d+),(\d+),(\d+);")
DEFAULT_BACKUP = "/var/lib/adl200a/gcc-backup.txt"


def talk(ser, cmd, wait=3.0, idle=0.8):
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
    return [l.strip().decode("latin1") for l in bytes(buf).split(b"\r") if l.strip()]


def read_config(ser):
    """{канал: (использовать, тип, температура, пар5, пар6)}"""
    lines = talk(ser, "GCC", wait=15.0, idle=1.2)
    cfg = {}
    for line in lines:
        m = GCC.search(line)
        if m:
            cfg[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
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


def write_channel(ser, ch, fields, commit):
    cmd = "SCC:%d,%d,%d,%d,%d,%d" % ((ch,) + tuple(fields))
    if not commit:
        print("  (сухой прогон) >>?1,%d,%s;" % (ARGS.id, cmd))
        return None
    reply = talk(ser, cmd)
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
    ap.add_argument("--template", default="1,1,1,20,6",
                    help="поля включённого канала: использовать,тип,температура,пар5,пар6")
    ap.add_argument("--backup", default=DEFAULT_BACKUP, help="куда сохранить копию GCC")
    ap.add_argument("--restore", help="вернуть конфигурацию из файла с копией GCC")
    ap.add_argument("--save", action="store_true",
                    help="послать SVE (сохранить в приборе); проверено не было")
    ap.add_argument("--yes", action="store_true", help="действительно писать в прибор")
    ARGS = ap.parse_args()

    ser = serial.Serial(ARGS.port, ARGS.baud, bytesize=8, parity="N", stopbits=1, timeout=0.5)
    try:
        cfg, lines = read_config(ser)
        if not cfg:
            raise SystemExit("прибор не ответил на GCC — порт занят службой?")
        print("=== сейчас в приборе ===")
        show(cfg)

        if ARGS.show:
            return

        targets = {}
        if ARGS.restore:
            for line in open(ARGS.restore):
                m = GCC.search(line)
                if m:
                    targets[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
            if not targets:
                raise SystemExit("в файле %s нет строк GCC" % ARGS.restore)
        else:
            tmpl = tuple(int(x) for x in ARGS.template.split(","))
            if len(tmpl) != 5:
                raise SystemExit("--template ожидает 5 чисел")
            chans = set()
            if ARGS.enable_all:
                chans = set(range(1, 17))
            elif ARGS.enable:
                chans = set(int(x) for x in ARGS.enable.split(","))
            for ch in sorted(chans):
                targets[ch] = tmpl
            for ch in (int(x) for x in ARGS.disable.split(",")) if ARGS.disable else ():
                targets[ch] = (0, 0, 0, 0, 0)

        if not targets:
            raise SystemExit("нечего менять: укажи --enable-all, --enable, --disable или --restore")

        todo = dict((ch, f) for ch, f in targets.items() if cfg.get(ch) != f)
        if not todo:
            print("\nвсё уже в нужном состоянии, писать нечего")
            return

        if ARGS.yes:
            path = ARGS.backup
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with open(path, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            print("\nрезервная копия: %s  (вернуть: --restore %s --yes)" % (path, path))

        print("\n=== запись (%d каналов) ===" % len(todo))
        for ch in sorted(todo):
            write_channel(ser, ch, todo[ch], ARGS.yes)

        if not ARGS.yes:
            print("\nсухой прогон — в прибор ничего не послано. Добавь --yes, чтобы записать.")
            return

        if ARGS.save:
            print("\nSVE: %s" % ("; ".join(talk(ser, "SVE")) or "(молчит)"))

        print("\n=== стало ===")
        again, _ = read_config(ser)
        show(again)
        bad = [ch for ch, f in todo.items() if again.get(ch) != f]
        if bad:
            print("\nНЕ ПРИМЕНИЛОСЬ на каналах: %s" %
                  ", ".join("%02d" % c for c in sorted(bad)))
            print("вернуть как было: python3 %s --restore %s --yes" % (sys.argv[0], ARGS.backup))
    finally:
        ser.close()


if __name__ == "__main__":
    main()
