#!/usr/bin/env python3
"""ace_config.py — channel and recording settings of the ACE ADL-200A.

Everything here is recovered from the vendor ADL2Pro.exe (see PROTOCOL.md):
the six fields of SCC/GCC, the VW sweep bands, the analog type codes — which are
also the TYPE field of MES frames — and the recording commands SAT/SAM/SOV.

The desired state lives in the same JSON file as the calibrations
(/etc/adl200a/sensors.json): every channel entry declares "sweep" and "analog",
and an optional "logger": {"recording": {...}} block sets the logger's own
recording. This module only knows the device; the math stays in calibration.py.
"""
import json
import os
import re

# VW sweep bands, field f4 (manual, "VW Channel Setup")
SWEEP_BANDS = {1: (450, 1200), 2: (800, 2000), 3: (1400, 3500), 4: (2400, 6000)}

# Field f5 whenever the VW input is on. ADL2Pro hard-codes it; meaning unknown.
VW_CONSTANT = 20

# Input type codes: field f6 for the analog input, and the TYPE field of MES
# frames for both inputs (VW always reports as TYPE 01).
TYPE_VW = 1
ANALOG_TYPES = {"mv": 2, "bridge": 3, "serial": 4, "resistance": 5,
                "thermistor_3k": 6, "rtd": 7}
TYPE_NAMES = {TYPE_VW: "vw"}
TYPE_NAMES.update((code, name) for name, code in ANALOG_TYPES.items())
TYPE_LABELS = {1: "VW", 2: "mV", 3: "Bridge", 4: "Serial sensor", 5: "Resistance",
               6: "Thermistor 3K", 7: "RTD"}
# What the integer in a MES frame means. Only the two seen on a live unit are
# filled in; the rest stay None until a sensor of that type is measured.
TYPE_UNITS = {1: "Hz x100", 6: "Ohm"}

# ADL2Pro forces the sweep to 0 when either of these is chosen.
EXCLUSIVE_ANALOG = (ANALOG_TYPES["bridge"], ANALOG_TYPES["serial"])

RECORDING_MODES = {"fixed": 1, "periodic": 2}          # SAM / GAM
WHEN_FULL = {"stop": 0, "overwrite": 1}                # SOV / GOV

OFF = (0, 0, 0, 0, 0)
GCC_RE = re.compile(r"GCC:(\d+),(\d+),(\d+),(\d+),(\d+),(\d+);")
GAT_RE = re.compile(r"GAT:(\d+),(\d+);")
GAM_RE = re.compile(r"GAM:(\d+);")
GOV_RE = re.compile(r"GOV:(\d+);")


class ConfigError(ValueError):
    pass


def band_label(sweep):
    lo, hi = SWEEP_BANDS[sweep]
    return "Sweep %02d (%d–%d Гц)" % (sweep, lo, hi)


def pick_sweep(hz_min, hz_max):
    """Narrowest sweep band that contains the sensor's whole frequency span.

    The span comes from the calibration sheet. A band that misses part of it
    excites the wire off resonance there: readings get unstable or vanish.
    """
    lo, hi = sorted((float(hz_min), float(hz_max)))
    fits = [s for s, (a, b) in SWEEP_BANDS.items() if a <= lo and hi <= b]
    if not fits:
        raise ConfigError("ни одна полоса не накрывает %.1f–%.1f Гц целиком; есть %s"
                          % (lo, hi, ", ".join(band_label(s) for s in sorted(SWEEP_BANDS))))
    return min(fits, key=lambda s: SWEEP_BANDS[s][1] - SWEEP_BANDS[s][0])


def covers(sweep, hz_min, hz_max):
    a, b = SWEEP_BANDS[sweep]
    lo, hi = sorted((float(hz_min), float(hz_max)))
    return a <= lo and hi <= b


def build_fields(sweep, analog, serial_count=None):
    """(f2..f6) for SCC, exactly as ADL2Pro's "Send setting" builds them."""
    sweep = int(sweep or 0)
    analog = int(analog or 0)
    if not sweep and not analog:
        return OFF
    if sweep not in SWEEP_BANDS and sweep != 0:
        raise ConfigError("полоса Sweep %d не существует, есть 1–4" % sweep)
    if analog and analog not in TYPE_LABELS:
        raise ConfigError("аналогового типа с кодом %d нет" % analog)
    if sweep and analog in EXCLUSIVE_ANALOG:
        raise ConfigError("%s нельзя совмещать с VW на одном канале" % TYPE_LABELS[analog])
    if analog == ANALOG_TYPES["serial"]:
        if not serial_count or int(serial_count) < 1:
            raise ConfigError("для serial-датчика нужен serial_count — сколько значений он отдаёт")
        f5 = int(serial_count)
    else:
        f5 = VW_CONSTANT if sweep else 0
    return (1, 1 if sweep else 0, sweep, f5, analog)


def describe(fields):
    """Human description of (f2..f6)."""
    use, vw, sweep, f5, analog = fields
    if not use:
        return "выключен"
    parts = []
    if vw:
        parts.append("VW " + (band_label(sweep) if sweep in SWEEP_BANDS
                              else "полоса %d?" % sweep))
    if analog:
        label = TYPE_LABELS.get(analog, "тип %d?" % analog)
        if analog == ANALOG_TYPES["serial"]:
            label += " ×%d" % f5
        parts.append(label)
    return " + ".join(parts) or "включён, но без входов"


def frame_types(fields):
    """MES TYPE codes a channel with these settings reports."""
    use, vw, _sweep, _f5, analog = fields
    out = []
    if use and vw:
        out.append(TYPE_VW)
    if use and analog:
        out.append(analog)
    return out


def scc_command(channel, fields):
    return "SCC:%d,%d,%d,%d,%d,%d" % ((channel,) + tuple(fields))


def _text(src):
    if isinstance(src, (bytes, bytearray)):
        return bytes(src).decode("latin1")
    if isinstance(src, str):
        return src
    return "\n".join(src)


def parse_gcc(src):
    """GCC answer (bytes, text or lines) -> {channel: (f2..f6)}."""
    return dict((int(m.group(1)), tuple(int(x) for x in m.groups()[1:]))
                for m in GCC_RE.finditer(_text(src)))


def parse_recording(src):
    """GAT/GAM/GOV answers -> {"GAT": (h, m), "GAM": n, "GOV": n}, whatever is found."""
    text = _text(src)
    out = {}
    m = GAT_RE.search(text)
    if m:
        out["GAT"] = (int(m.group(1)), int(m.group(2)))
    m = GAM_RE.search(text)
    if m:
        out["GAM"] = int(m.group(1))
    m = GOV_RE.search(text)
    if m:
        out["GOV"] = int(m.group(1))
    return out


def describe_recording(rec):
    parts = []
    if "GAT" in rec:
        h, m = rec["GAT"]
        parts.append("интервал %d ч %d мин" % (h, m))
    if "GAM" in rec:
        modes = dict((v, k) for k, v in RECORDING_MODES.items())
        parts.append("режим %s" % modes.get(rec["GAM"], rec["GAM"]))
    if "GOV" in rec:
        full = {0: "останавливать запись", 1: "перезаписывать старое"}
        parts.append("при заполнении памяти — %s" % full.get(rec["GOV"], rec["GOV"]))
    return ", ".join(parts) or "не прочитано"


def recording_commands(want):
    """{"GAT": (h, m), "GAM": n, "GOV": n} -> the set commands, in that order."""
    out = []
    if "GAT" in want:
        out.append(("GAT", "SAT:%d,%d" % want["GAT"]))
    if "GAM" in want:
        out.append(("GAM", "SAM:%d" % want["GAM"]))
    if "GOV" in want:
        out.append(("GOV", "SOV:%d" % want["GOV"]))
    return out


def decode(cfg):
    """{channel: fields} -> JSON-friendly description of the enabled channels.

    Published retained on <topic>/config, so whoever reads the raw MES values
    knows what t01, t06 ... mean on every channel.
    """
    out = []
    for ch in sorted(cfg):
        f = cfg[ch]
        if not f[0]:
            continue
        item = {"channel": ch, "setting": describe(f), "fields": list(f), "values": {}}
        if f[1]:
            lo, hi = SWEEP_BANDS.get(f[2], (None, None))
            item["vw"] = {"sweep": f[2], "band_hz": [lo, hi]}
        if f[4]:
            item["analog"] = {"code": f[4], "type": TYPE_NAMES.get(f[4]),
                              "label": TYPE_LABELS.get(f[4])}
        for code in frame_types(f):
            item["values"]["t%02d" % code] = {"input": TYPE_NAMES.get(code),
                                               "unit": TYPE_UNITS.get(code)}
        out.append(item)
    return out


# ---------- desired state from sensors.json ----------

class ChannelPlan(object):
    """What one channel of the logger should be set to."""

    def __init__(self, channel, name, sweep, sweep_cfg, analog, serial_count, fields):
        self.channel = channel
        self.name = name
        self.sweep = sweep              # resolved band, 0 = VW off
        self.sweep_cfg = sweep_cfg      # as written in the file ("auto", 3, null)
        self.analog = analog            # code, 0 = analog off
        self.serial_count = serial_count
        self.fields = fields

    def describe(self):
        text = describe(self.fields)
        if self.sweep_cfg == "auto" and self.sweep:
            text += "  [полоса выбрана по сертификату]"
        return text


def _plan_channel(ch, cfg, problems):
    name = cfg.get("name", "канал %02d" % ch)
    where = "канал %02d (%s)" % (ch, name)
    missing = [k for k in ("sweep", "analog") if k not in cfg]
    if missing:
        problems.append("%s: не указано %s — допиши в файл "
                        "(null, если вход не используется)" % (where, " и ".join(missing)))
        return None

    hz_min, hz_max = cfg.get("hz_min"), cfg.get("hz_max")
    have_span = hz_min is not None and hz_max is not None
    sweep_cfg = cfg.get("sweep")
    sweep = 0
    if sweep_cfg in (None, 0, False):
        sweep = 0
    elif sweep_cfg == "auto":
        if not have_span:
            problems.append("%s: sweep \"auto\" требует hz_min и hz_max из сертификата" % where)
            return None
        try:
            sweep = pick_sweep(hz_min, hz_max)
        except ConfigError as e:
            problems.append("%s: %s" % (where, e))
            return None
    else:
        try:
            sweep = int(sweep_cfg)
        except (TypeError, ValueError):
            sweep = -1
        if sweep not in SWEEP_BANDS:
            problems.append("%s: sweep должен быть \"auto\", 1–4 или null, а не %r"
                            % (where, sweep_cfg))
            return None
        if have_span and not covers(sweep, hz_min, hz_max):
            try:
                hint = "подходит %s" % band_label(pick_sweep(hz_min, hz_max))
            except ConfigError:
                hint = "целиком его не накрывает ни одна полоса"
            problems.append("%s: %s не накрывает рабочий диапазон датчика %s–%s Гц; %s"
                            % (where, band_label(sweep), hz_min, hz_max, hint))
            return None

    analog_cfg = cfg.get("analog")
    if analog_cfg in (None, False, "", "none"):
        analog = 0
    elif analog_cfg in ANALOG_TYPES:
        analog = ANALOG_TYPES[analog_cfg]
    else:
        problems.append("%s: analog %r неизвестен, есть %s или null"
                        % (where, analog_cfg, ", ".join(sorted(ANALOG_TYPES))))
        return None

    try:
        fields = build_fields(sweep, analog, cfg.get("serial_count"))
    except ConfigError as e:
        problems.append("%s: %s" % (where, e))
        return None
    return ChannelPlan(ch, name, sweep, sweep_cfg, analog, cfg.get("serial_count"), fields)


def _plan_recording(rec, problems):
    want = {}
    try:
        interval = int(rec["interval_min"])
    except (KeyError, TypeError, ValueError):
        problems.append("logger.recording: нужен interval_min — интервал записи в минутах")
        interval = None
    if interval is not None:
        if not 1 <= interval <= 1440:
            problems.append("logger.recording: interval_min должен быть от 1 до 1440")
        else:
            want["GAT"] = (interval // 60, interval % 60)
    mode = rec.get("mode")
    if mode not in RECORDING_MODES:
        problems.append("logger.recording: mode должен быть %s"
                        % " или ".join('"%s"' % m for m in RECORDING_MODES))
    else:
        want["GAM"] = RECORDING_MODES[mode]
    full = rec.get("when_full")
    if full not in WHEN_FULL:
        problems.append("logger.recording: when_full должен быть %s"
                        % " или ".join('"%s"' % m for m in WHEN_FULL))
    else:
        want["GOV"] = WHEN_FULL[full]
    return want


def load_plan(path, strict=True):
    """sensors.json -> ({channel: ChannelPlan}, recording wish or None, problems).

    strict: every channel entry must declare sweep and analog (for `apply`).
    Not strict: entries without them are skipped quietly (for display).
    """
    if not path or not os.path.exists(path):
        return {}, None, ["файла %s нет" % path]
    with open(path) as fh:
        try:
            doc = json.load(fh)
        except ValueError as e:
            return {}, None, ["%s: не JSON — %s" % (path, e)]
    channels = doc.get("channels", {})
    plans, problems = {}, []
    for key, cfg in channels.items():
        if str(key).startswith("_") or not isinstance(cfg, dict):
            continue
        try:
            ch = int(key)
        except ValueError:
            problems.append("ключ канала %r — не число" % key)
            continue
        if not 1 <= ch <= 16:
            problems.append("канала %d нет, есть 1–16" % ch)
            continue
        if not strict and not ("sweep" in cfg and "analog" in cfg):
            continue
        plan = _plan_channel(ch, cfg, problems)
        if plan is not None:
            plans[ch] = plan

    recording = None
    rec = (doc.get("logger") or {}).get("recording")
    if rec is not None:
        if not isinstance(rec, dict):
            problems.append("logger.recording должен быть объектом")
        else:
            recording = _plan_recording(rec, problems)
    return plans, recording, problems
