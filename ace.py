#!/usr/bin/env python3
"""ace.py <CMD> [ID] [wait_s] — send one ACE command to the ADL-200A and print the reply.

  frame  = >>?1,<ID>,<CMD>;\\r
  reply  = <<!01,<ID>,<CMD>:<data>;\\r   (one or many lines)

Port/baud come from ADL200A_PORT / ADL200A_BAUD (defaults /dev/ttyUSB0, 38400).

Examples:
  ace.py GTT            # get time
  ace.py GCC            # channel config (16 lines)
  ace.py MEA            # measure all channels (~26 s)
Safe read cmds: GTT GDD GMU GMC GCC GAM GAT GOV MEA.
DANGER (change state): RUN FMT RST SVE and any S** / SDD / STT.
"""
import os
import sys
import time
import serial

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))

cmd = (sys.argv[1] if len(sys.argv) > 1 else "GTT").upper()
lid = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("ADL200A_ID", "1"))
wait = float(sys.argv[3]) if len(sys.argv) > 3 else (35.0 if cmd == "MEA" else 1.2)

frame = (">>?1,%d,%s;\r" % (lid, cmd)).encode()
s = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.5)
s.reset_input_buffer()
s.write(frame)
s.flush()

buf = bytearray()
t0 = time.time()
last = t0
while time.time() - t0 < wait:
    d = s.read(1024)
    if d:
        buf += d
        last = time.time()
    elif buf and time.time() - last > 2:
        break
s.close()

sys.stderr.write("TX %r on %s  (%d bytes RX)\n" % (frame, PORT, len(buf)))
for line in bytes(buf).split(b"\r"):
    line = line.strip()
    if line:
        print(line.decode("latin1"))
