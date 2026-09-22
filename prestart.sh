#!/bin/sh
# prestart.sh — put a Moxa UPort 1150 into RS-232 mode (setserial port 0).
# No-op on native RS-232 ports (/dev/ttyS*) or any non-Moxa adapter.
P="${ADL200A_PORT:-/dev/ttyUSB0}"
if command -v udevadm >/dev/null 2>&1 && \
   udevadm info -q property -n "$P" 2>/dev/null | grep -q "ID_MODEL_ID=1150"; then
    setserial "$P" port 0 2>/dev/null || true
fi
exit 0
