#!/usr/bin/env python3
"""sniffer.py <hex> — send raw hex bytes to the serial port, print the raw hex reply.

Port/baud from ADL200A_PORT / ADL200A_BAUD (defaults /dev/ttyUSB0, 38400).
Generic low-level probe; for the ACE ASCII protocol use ace.py instead.

  sniffer.py 3e 3e 3f 31 2c 31 2c 47 54 54 3b 0d     # >>?1,1,GTT;\\r
"""
import os
import sys
import serial

PORT = os.environ.get("ADL200A_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("ADL200A_BAUD", "38400"))
TIMEOUT = 2
MAXLEN = 512


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: sniffer.py <hex bytes>", file=sys.stderr)
        sys.exit(1)
    req = bytes.fromhex("".join(sys.argv[1:]))
    with serial.Serial(PORT, BAUD, bytesize=serial.EIGHTBITS,
                       parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                       timeout=TIMEOUT) as ser:
        ser.reset_input_buffer()
        ser.write(req)
        ser.flush()
        resp = ser.read(MAXLEN)
    print("TX[%d] %s" % (len(req), req.hex(" ")), file=sys.stderr)
    print(resp.hex(" ") if resp else "<no response / timeout>")


if __name__ == "__main__":
    main()
