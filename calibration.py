#!/usr/bin/env python3
"""calibration.py — ADL-200A raw counts -> engineering units.

Each MEA frame carries one integer per channel input; its TYPE is the input code:

  TYPE 01 — vibrating-wire reading. The scale depends on the logger's measuring
            mode, so it is resolved per sensor (see ``raw_unit`` below).
  TYPE 06 — thermistor resistance in ohms (a 3 kOhm @ 25 C bead reads ~3007).
  TYPE 02, 03, 04, 05, 07 — mV, Bridge, Serial, Resistance, RTD. What their
            integers mean has not been measured on a live unit, so the scale
            is set by the installer in "analog_scale" (see AnalogScale).

Sensor definitions are typed in from the manufacturer's calibration sheet and
live in a JSON file keyed by logger channel — see sensors.example.json.

Standalone use:

    python3 calibration.py --channel 02 --raw 82233 --therm 3007
    python3 calibration.py --channel 05 --analog 12345   # mV, RTD, ... input
    python3 calibration.py --explain 82233        # all candidate scalings
    python3 calibration.py --check                # validate the whole file
"""
import json
import math
import os

import ace_config as ace

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


# Platinum RTD, IEC 60751 (alpha = 0.00385): Callendar–Van Dusen coefficients.
CVD_A, CVD_B, CVD_C = 3.9083e-3, -5.775e-7, -4.183e-12
RTD_R0 = {"pt100": 100.0, "pt1000": 1000.0}


def _rtd_ohms(t, r0):
    r = 1.0 + CVD_A * t + CVD_B * t * t
    if t < 0:
        r += CVD_C * (t - 100.0) * t ** 3
    return r0 * r


def rtd_to_celsius(ohms, r0):
    """Platinum RTD resistance -> temperature (-200..+850 C), or None outside it."""
    if ohms is None or not _rtd_ohms(-200.0, r0) <= ohms <= _rtd_ohms(850.0, r0):
        return None
    # exact for t >= 0; below zero refine with Newton on the full equation
    t = (-CVD_A + math.sqrt(CVD_A ** 2 - 4 * CVD_B * (1 - ohms / r0))) / (2 * CVD_B)
    if ohms < r0:
        for _ in range(20):
            f = _rtd_ohms(t, r0) - ohms
            h = 1e-4
            d = (_rtd_ohms(t + h, r0) - _rtd_ohms(t - h, r0)) / (2 * h)
            t -= f / d
            if abs(f) < 1e-9:
                break
    return t


class AnalogScale(object):
    """Installer-defined scale for an analog input: mV, Bridge, Resistance, RTD, Serial.

        raw integer  x raw_multiplier  ->  input value, in input_unit (mV, mV/V, Ohm ...)
        input value  -> linear | polynomial | function  ->  value, in unit

    raw_multiplier is found on site against a reference (calibrator, precision
    resistor); the conversion comes from the sensor's own calibration sheet.
    With no conversion given, only the input value is shown — which is exactly
    what is needed while raw_multiplier is being found.
    """

    FUNCTIONS = tuple(sorted(RTD_R0))

    def __init__(self, cfg):
        self.raw_multiplier = float(cfg.get("raw_multiplier", 1.0))
        self.input_unit = cfg.get("input_unit", "")
        self.unit = cfg.get("unit", "")
        self.quantity = cfg.get("quantity", "")
        self.function = cfg.get("function")
        self.polynomial = cfg.get("polynomial")
        lin = cfg.get("linear") or {}
        self.gage_factor = lin.get("gage_factor")
        self.zero_reading = lin.get("zero_reading", 0.0)

    @staticmethod
    def problems(cfg):
        """What is wrong with an analog_scale block, as a list of strings."""
        if not isinstance(cfg, dict):
            return ["analog_scale должен быть объектом"]
        out = []
        m = cfg.get("raw_multiplier", 1.0)
        if not isinstance(m, (int, float)) or isinstance(m, bool) or m == 0:
            out.append("raw_multiplier должен быть ненулевым числом")
        given = [k for k in ("linear", "polynomial", "function") if cfg.get(k) is not None]
        if len(given) > 1:
            out.append("задай что-то одно из linear, polynomial, function, а не %s"
                       % " и ".join(given))
        fn = cfg.get("function")
        if fn is not None and fn not in RTD_R0:
            out.append("function: %s, а не %r" % (" или ".join(sorted(RTD_R0)), fn))
        poly = cfg.get("polynomial")
        if poly is not None and (not isinstance(poly, list) or not poly or not all(
                isinstance(c, (int, float)) and not isinstance(c, bool) for c in poly)):
            out.append("polynomial — список чисел [c0, c1, c2, …]: значение = c0 + c1·x + c2·x²…")
        lin = cfg.get("linear")
        if lin is not None and (not isinstance(lin, dict) or not isinstance(
                lin.get("gage_factor"), (int, float))):
            out.append("linear — {\"gage_factor\": G, \"zero_reading\": Z}: значение = G·(x − Z)")
        return out

    def convert(self, raw):
        out = {"raw": raw, "status": "ok"}
        if raw is None:
            out["status"] = "no_value"
            return out
        x = raw * self.raw_multiplier
        out["input"] = round(x, 6)
        if self.input_unit:
            out["input_unit"] = self.input_unit
        value = None
        if self.function:
            value = rtd_to_celsius(x, RTD_R0[self.function])
            if value is None:
                out["status"] = "out_of_range"
                return out
            out["unit"] = self.unit or "°C"
        elif self.gage_factor is not None:
            value = self.gage_factor * (x - (self.zero_reading or 0.0))
        elif self.polynomial:
            value = sum(c * x ** i for i, c in enumerate(self.polynomial))
        else:
            out["status"] = "input_only"
            return out
        out["value"] = round(value, 6)
        if self.unit and "unit" not in out:
            out["unit"] = self.unit
        if self.quantity:
            out["quantity"] = self.quantity
        return out


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

        # Inputs of the channel. Files written before "sweep" existed describe
        # VW sensors only, so a missing key means VW is on.
        self.has_vw = cfg.get("sweep", "auto") not in (None, 0, False)
        self.analog_code = ace.ANALOG_TYPES.get(cfg.get("analog") or "", 0)
        scale = cfg.get("analog_scale")
        self.analog_scale = None
        if (isinstance(scale, dict) and not AnalogScale.problems(scale)
                and self.analog_code not in (0, ace.ANALOG_TYPES["thermistor_3k"])):
            self.analog_scale = AnalogScale(scale)

    def analog_key(self):
        """MES key of the channel's analog input other than the thermistor, or None."""
        if self.analog_code in (0, ace.ANALOG_TYPES["thermistor_3k"]):
            return None
        return "t%02d" % self.analog_code

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

    def convert_values(self, values):
        """Every input of the channel -> engineering dict: VW, thermistor, other analog.

        values is the MES dict of one channel, {"t01": ..., "t06": ..., "t02": ...}.
        """
        if self.has_vw:
            out = self.convert(values.get("t01"), values.get("t06"))
        else:
            out = {"sensor": self.name, "status": "ok"}
            for key in ("model", "serial"):
                if getattr(self, key):
                    out[key] = getattr(self, key)
            ohms = values.get("t06") or None
            temp_c = resistance_to_celsius(ohms, self.thermistor)
            if ohms is not None:
                out["thermistor_ohms"] = ohms
            if temp_c is not None:
                out["temperature_c"] = round(temp_c, 2)
            elif self.analog_code == ace.ANALOG_TYPES["thermistor_3k"]:
                out["status"] = "disconnected"
        key = self.analog_key()
        if key:
            if self.analog_scale is not None:
                out["analog"] = self.analog_scale.convert(values.get(key))
            else:
                out["analog"] = {"raw": values.get(key), "status": "no_scale"}
            out["analog"]["label"] = ace.TYPE_LABELS[self.analog_code]
            out["analog"]["key"] = key
            if not self.has_vw and out["analog"]["status"] not in ("ok", "input_only"):
                out["status"] = out["analog"]["status"]
        return out


def check_file(path):
    """Everything wrong with a sensors file: channel inputs and installer scales."""
    _plans, _rec, problems = ace.load_plan(path, strict=True)
    if not path or not os.path.exists(path):
        return problems, []
    notes = []
    with open(path) as fh:
        doc = json.load(fh)
    for key, cfg in sorted(doc.get("channels", {}).items()):
        if str(key).startswith("_") or not isinstance(cfg, dict):
            continue
        where = "канал %s (%s)" % (key, cfg.get("name", "без имени"))
        code = ace.ANALOG_TYPES.get(cfg.get("analog") or "", 0)
        scale = cfg.get("analog_scale")
        if scale is not None:
            if code in (0, ace.ANALOG_TYPES["thermistor_3k"]):
                notes.append("%s: analog_scale не нужен — %s" % (
                    where, "аналоговый вход выключен" if not code
                    else "у Thermistor 3K своя кривая"))
            for p in AnalogScale.problems(scale):
                problems.append("%s: analog_scale: %s" % (where, p))
        elif code and code != ace.ANALOG_TYPES["thermistor_3k"]:
            notes.append("%s: у входа %s нет analog_scale — будет видно только сырое число"
                         % (where, ace.TYPE_LABELS[code]))
    return problems, notes


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
    ap.add_argument("--analog", type=int, metavar="RAW",
                    help="integer of the channel's other analog input (mV, Bridge, RTD ...)")
    ap.add_argument("--check", action="store_true",
                    help="validate the whole file: channel inputs and analog scales")
    ap.add_argument("--explain", type=int, metavar="RAW",
                    help="show every candidate scaling for one TYPE 01 integer")
    ap.add_argument("--types", action="store_true",
                    help="list the MES TYPE codes and what their integers mean")
    args = ap.parse_args()

    if args.types:
        print("TYPE  input            value")
        for code in sorted(ace.TYPE_LABELS):
            print("  %02d  %-15s  %s" % (code, ace.TYPE_LABELS[code],
                                       ace.TYPE_UNITS.get(code) or "not measured yet"))
        return

    if args.check:
        problems, notes = check_file(args.sensors)
        for n in notes:
            print("заметка: " + n)
        if problems:
            raise SystemExit("ошибки в %s:\n  - %s" % (args.sensors, "\n  - ".join(problems)))
        print("%s в порядке" % args.sensors)
        return

    if args.explain is not None:
        print("raw TYPE 01 (VW) = %d" % args.explain)
        for unit, hz in sorted(candidate_frequencies(args.explain).items()):
            print("  %-11s -> %9.2f Hz   %10.2f digits" % (unit, hz, hz * hz / 1000.0))
        return

    sensors = load_sensors(args.sensors)
    if not sensors:
        raise SystemExit("no sensor definitions in %s" % args.sensors)
    plans, _rec, _problems = ace.load_plan(args.sensors, strict=False)
    if args.channel is None:
        for ch in sorted(sensors):
            s = sensors[ch]
            inputs = plans[ch].describe() if ch in plans else "inputs not declared"
            print("ch %02d  %s  model %s  s/n %s  —  %s" % (ch, s.name, s.model, s.serial, inputs))
        return
    s = sensors.get(args.channel)
    if s is None:
        raise SystemExit("channel %02d is not defined in %s" % (args.channel, args.sensors))
    values = {"t01": args.raw, "t06": args.therm}
    if s.analog_key():
        values[s.analog_key()] = args.analog
    out = s.convert_values(values)
    if args.channel in plans:
        out["inputs"] = dict(("t%02d" % code, "%s, %s" % (ace.TYPE_LABELS[code],
                                                          ace.TYPE_UNITS.get(code) or "?"))
                             for code in ace.frame_types(plans[args.channel].fields))
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
