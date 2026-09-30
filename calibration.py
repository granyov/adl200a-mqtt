#!/usr/bin/env python3
"""calibration.py — ADL-200A raw counts -> engineering units.

Each MEA frame carries two integers per channel:

  TYPE 01 — vibrating-wire reading. The scale depends on the logger's measuring
            mode, so it is resolved per sensor (see ``raw_unit`` below).
  TYPE 06 — thermistor resistance in ohms (a 3 kOhm @ 25 C bead reads ~3007).

Sensor definitions are typed in from the manufacturer's calibration sheet and
live in a JSON file keyed by logger channel — see sensors.example.json.

Standalone use:

    python3 calibration.py --channel 02 --raw 82233 --therm 3007
    python3 calibration.py --explain 82233        # all candidate scalings
"""
import json
import math
import os

# Steinhart-Hart constants for the 3 kOhm @ 25 C bead used by ACE Instrument
# (identical to the Geokon/Slope Indicator standard curve, -40..+105 C).
THERMISTOR_3K = (1.4051e-3, 2.369e-4, 1.019e-7)

# Resistance window the bead can physically show over its rated span.
# Outside it the pair is open (no sensor / cut cable) or shorted.
THERM_R_MIN = 150.0
THERM_R_MAX = 110000.0

KGF_CM2_TO_KPA = 98.0665

# How the TYPE 01 integer maps to a vibrating-wire frequency in Hz.
RAW_UNITS = {
    "hz":         lambda r: float(r),
    "hz_x100":    lambda r: r / 100.0,
    "digits":     lambda r: math.sqrt(r * 1000.0),
    "digits_x10": lambda r: math.sqrt(r * 100.0),
}


def resistance_to_celsius(ohms, coeffs=THERMISTOR_3K):
    """Thermistor resistance -> temperature, or None when out of range."""
    if ohms is None or not THERM_R_MIN <= ohms <= THERM_R_MAX:
        return None
    a, b, c = coeffs
    ln_r = math.log(ohms)
    return 1.0 / (a + b * ln_r + c * ln_r ** 3) - 273.15


def candidate_frequencies(raw):
    """Every plausible reading of one TYPE 01 integer, as {unit: Hz}."""
    if raw is None or raw <= 0:
        return {}
    return dict((name, fn(raw)) for name, fn in RAW_UNITS.items())


class Sensor(object):
    """One calibrated sensor on one logger channel."""

    def __init__(self, channel, cfg):
        self.channel = int(channel)
        self.name = cfg.get("name", "channel %02d" % self.channel)
        self.model = cfg.get("model")
        self.serial = cfg.get("serial")
        self.unit = cfg.get("unit", "kgf/cm2")
        self.full_scale = cfg.get("range")
        self.cal_temp_c = cfg.get("cal_temp_c")
        self.initial_temp_c = cfg.get("initial_temp_c")
        if self.initial_temp_c is None:
            self.initial_temp_c = self.cal_temp_c
        self.tct = cfg.get("thermal_coefficient", 0.0)
        self.thermistor = THERMISTOR_3K
        if isinstance(cfg.get("thermistor"), dict):
            t = cfg["thermistor"]
            self.thermistor = (t["A"], t["B"], t["C"])

        poly = cfg.get("polynomial") or {}
        self.poly = (poly.get("A"), poly.get("B"), poly.get("C"))
        lin = cfg.get("linear") or {}
        self.gage_factor = lin.get("gage_factor")
        self.zero_reading = lin.get("zero_reading")

        # Calibrated frequency span, used to resolve raw_unit="auto".
        self.hz_min = cfg.get("hz_min")
        self.hz_max = cfg.get("hz_max")
        self.raw_unit = cfg.get("raw_unit", "auto")
        self._resolved_unit = None if self.raw_unit == "auto" else self.raw_unit

    @property
    def resolved_unit(self):
        return self._resolved_unit

    def _hz_window(self):
        """Accept readings a bit outside the calibrated span (over/under range)."""
        if self.hz_min is None or self.hz_max is None:
            return None
        return self.hz_min * 0.7, self.hz_max * 1.3

    def resolve_unit(self, raw):
        """Decide (once) how to read TYPE 01, latching the result.

        The four candidate scalings differ by more than 10x, so the calibrated
        frequency window picks exactly one of them for a real sensor.
        Returns (unit, note); unit is None when the reading stays ambiguous.
        """
        if self._resolved_unit:
            return self._resolved_unit, None
        window = self._hz_window()
        if window is None:
            return None, "no hz_min/hz_max in the sensor definition"
        lo, hi = window
        hits = [u for u, f in candidate_frequencies(raw).items() if lo <= f <= hi]
        if len(hits) == 1:
            self._resolved_unit = hits[0]
            return hits[0], "resolved from %d against %.0f..%.0f Hz" % (raw, lo, hi)
        if not hits:
            return None, "%d matches no scaling inside %.0f..%.0f Hz" % (raw, lo, hi)
        return None, "%d is ambiguous: %s" % (raw, ", ".join(sorted(hits)))

    def frequency(self, raw):
        unit = self._resolved_unit
        if not unit or raw is None or raw <= 0:
            return None
        return RAW_UNITS[unit](raw)

    def pressure(self, hz, temp_c=None):
        """Sheet formula: P = A*Hz^2 + B*Hz + C - Tct*(Tc - Ti)."""
        a, b, c = self.poly
        if hz is None or a is None:
            return None
        return a * hz * hz + b * hz + c - self._temp_correction(temp_c)

    def pressure_linear(self, digits, temp_c=None):
        """Sheet formula: P = G*(reading - zero) - Tct*(Tc - Ti)."""
        if digits is None or self.gage_factor is None:
            return None
        return (self.gage_factor * (digits - self.zero_reading)
                - self._temp_correction(temp_c))

    def _temp_correction(self, temp_c):
        if temp_c is None or self.initial_temp_c is None or not self.tct:
            return 0.0
        return self.tct * (temp_c - self.initial_temp_c)

    def convert(self, t01, t06):
        """Raw pair -> engineering dict. Never raises; reports status instead."""
        out = {"sensor": self.name, "status": "ok"}
        if self.model:
            out["model"] = self.model
        if self.serial:
            out["serial"] = self.serial

        ohms = t06 if t06 else None
        temp_c = resistance_to_celsius(ohms, self.thermistor)
        if temp_c is not None:
            out["temperature_c"] = round(temp_c, 2)
        out["thermistor_ohms"] = ohms

        if not t01:
            out["status"] = "no_vw_signal"
            if temp_c is None:
                out["status"] = "disconnected"
            return out

        unit, note = self.resolve_unit(t01)
        if unit is None:
            out["status"] = "unresolved_scale"
            out["note"] = note
            out["candidates_hz"] = dict(
                (u, round(f, 2)) for u, f in candidate_frequencies(t01).items())
            return out

        hz = self.frequency(t01)
        digits = hz * hz / 1000.0
        out["raw_unit"] = unit
        out["frequency_hz"] = round(hz, 2)
        out["digits"] = round(digits, 2)

        if temp_c is None:
            out["status"] = "no_temperature"
            out["note"] = "thermistor open or out of range; no thermal correction"

        p = self.pressure(hz, temp_c)
        if p is not None:
            out["pressure"] = round(p, 4)
            out["pressure_unit"] = self.unit
            if self.unit == "kgf/cm2":
                out["pressure_kpa"] = round(p * KGF_CM2_TO_KPA, 3)
            lin = self.pressure_linear(digits, temp_c)
            if lin is not None:
                out["pressure_linear"] = round(lin, 4)
            if self.full_scale:
                out["percent_fsr"] = round(100.0 * p / self.full_scale, 2)
        return out


def load_sensors(path):
    """Read a sensors JSON file -> {channel:int -> Sensor}. {} when absent."""
    if not path or not os.path.exists(path):
        return {}
    with open(path) as fh:
        doc = json.load(fh)
    channels = doc.get("channels", doc)
    out = {}
    for ch, cfg in channels.items():
        if str(ch).startswith("_") or not isinstance(cfg, dict):
            continue
        out[int(ch)] = Sensor(ch, cfg)
    return out


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="ADL-200A raw counts -> engineering units")
    ap.add_argument("--sensors", default=os.environ.get(
        "ADL200A_SENSORS", "/etc/adl200a/sensors.json"))
    ap.add_argument("--channel", type=int)
    ap.add_argument("--raw", type=int, help="TYPE 01 integer")
    ap.add_argument("--therm", type=int, help="TYPE 06 integer (ohms)")
    ap.add_argument("--explain", type=int, metavar="RAW",
                    help="show every candidate scaling for one TYPE 01 integer")
    args = ap.parse_args()

    if args.explain is not None:
        print("raw TYPE 01 = %d" % args.explain)
        for unit, hz in sorted(candidate_frequencies(args.explain).items()):
            print("  %-11s -> %9.2f Hz   %10.2f digits" % (unit, hz, hz * hz / 1000.0))
        return

    sensors = load_sensors(args.sensors)
    if not sensors:
        raise SystemExit("no sensor definitions in %s" % args.sensors)
    if args.channel is None:
        for ch in sorted(sensors):
            s = sensors[ch]
            print("ch %02d  %s  model %s  s/n %s" % (ch, s.name, s.model, s.serial))
        return
    s = sensors.get(args.channel)
    if s is None:
        raise SystemExit("channel %02d is not defined in %s" % (args.channel, args.sensors))
    print(json.dumps(s.convert(args.raw, args.therm), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
