#!/usr/bin/env python3
"""fake_logger.py — ADL-200A emulator on a pseudo-terminal, for tests without the device.

    python3 tests/fake_logger.py --preset office --sensor 1:2947.2:3344
    -> prints the pty path (e.g. /dev/ttys012); point ADL200A_PORT / --port at it.

Speaks the ACE frames recovered from ADL2Pro.exe: GTT, GCC/SCC, GAT/SAT,
GAM/SAM, GOV/SOV, SVE and MEA. MEA answers channel by channel with a delay, like
the real logger. A VW reading comes back only when the channel's sweep band
contains the sensor's frequency — the failure mode we hit with Sweep 01.
"""
import argparse
import os
import re
import select
import sys
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ace_config as ace

REQ = re.compile(rb">>\?1,(\d+),([A-Z]{3})(?::([^;]*))?;")
OPEN_OHMS = 239500          # what an open thermistor input reads on the real unit

PRESETS = {
    # as shipped: channels 02 and 04, ADL2Pro defaults (Sweep 01 + Thermistor 3K)
    "factory": dict((ch, (1, 1, 1, 20, 6)) for ch in (2, 4)),
    # the office unit on 2026-09-30 after --enable-all with the old fixed template
    "office": dict((ch, (1, 1, 1, 20, 6)) for ch in range(1, 15)),
}


class Logger(object):
    def __init__(self, args):
        self.id = args.id
        self.delay = args.per_channel
        self.cfg = dict((ch, ace.OFF) for ch in range(1, 17))
        self.cfg.update(PRESETS[args.preset])
        self.rec = {"GAT": (0, 1), "GAM": 1, "GOV": 1}
        self.sensors = {}
        for spec in args.sensor:
            ch, hz, ohm = spec.split(":")
            self.sensors[int(ch)] = (float(hz), int(ohm))

    def reply(self, body):
        return ("<<!01,%02d,%s;\r" % (self.id, body)).encode()

    def handle(self, cmd, arg, out):
        if cmd == "GTT":
            out(self.reply("GTT:" + time.strftime("%H:%M:%S")))
        elif cmd == "GCC":
            for ch in range(1, 17):
                f = self.cfg[ch]
                out(self.reply("GCC:%02d,%d,%d,%d,%04d,%d" % ((ch,) + f)))
        elif cmd == "SCC":
            try:
                v = [int(x) for x in arg.split(",")]
                assert len(v) == 6 and 1 <= v[0] <= 16
            except (ValueError, AssertionError, AttributeError):
                return out(self.reply("NAK:04"))
            self.cfg[v[0]] = tuple(v[1:])
            out(self.reply("ACK"))
        elif cmd in ("GAT", "GAM", "GOV"):
            val = self.rec[cmd]
            body = "%02d,%02d" % val if cmd == "GAT" else "%02d" % val
            out(self.reply("%s:%s" % (cmd, body)))
        elif cmd in ("SAT", "SAM", "SOV"):
            try:
                v = [int(x) for x in arg.split(",")]
            except (ValueError, AttributeError):
                return out(self.reply("NAK:04"))
            key = "G" + cmd[1:]
            self.rec[key] = (v[0], v[1]) if cmd == "SAT" else v[0]
            out(self.reply("ACK"))
        elif cmd == "SVE":
            out(self.reply("ACK"))
        elif cmd == "MEA":
            self.measure(out)
        else:
            out(self.reply("NAK:04"))

    def measure(self, out):
        stamp = time.strftime("%y/%m/%d,%H:%M:%S")
        for ch in range(1, 17):
            f = self.cfg[ch]
            if not f[0]:
                continue
            time.sleep(self.delay)
            hz, ohm = self.sensors.get(ch, (0.0, OPEN_OHMS))
            for code in ace.frame_types(f):
                if code == ace.TYPE_VW:
                    a, b = ace.SWEEP_BANDS.get(f[2], (0, 0))
                    value = int(round(hz * 100)) if a <= hz <= b else 0
                elif code == ace.ANALOG_TYPES["thermistor_3k"]:
                    value = ohm
                else:
                    value = 0
                out(self.reply("MES:%02d,%02d,%s,%010d" % (ch, code, stamp, value)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--id", type=int, default=1)
    ap.add_argument("--preset", choices=sorted(PRESETS), default="factory")
    ap.add_argument("--sensor", action="append", default=[],
                    help="CH:HZ:OHM — a VW sensor with a thermistor on channel CH")
    ap.add_argument("--per-channel", type=float, default=0.3,
                    help="seconds per channel during MEA (the real unit: ~5)")
    ap.add_argument("--log", action="store_true", help="echo requests to stderr")
    args = ap.parse_args()

    master, slave = os.openpty()
    tty.setraw(slave)
    print(os.ttyname(slave), flush=True)
    dev = Logger(args)

    def out(data):
        os.write(master, data)

    buf = b""
    while True:
        r, _, _ = select.select([master], [], [], 1.0)
        if not r:
            continue
        try:
            buf += os.read(master, 1024)
        except OSError:
            continue
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            m = REQ.search(line)
            if not m:
                continue
            if args.log:
                sys.stderr.write("<- %s\n" % line.decode("latin1"))
            if int(m.group(1)) != dev.id:
                continue
            arg = m.group(3).decode("latin1") if m.group(3) is not None else None
            dev.handle(m.group(2).decode(), arg, out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
